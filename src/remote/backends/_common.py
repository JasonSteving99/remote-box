"""Helpers shared by the cloud sandbox backends (E2B, Daytona)."""

import fnmatch
import hashlib
import os
import posixpath
from functools import cache
from pathlib import Path

from pydantic import SecretStr


def require_api_key(config_value: SecretStr | None, env_var: str, backend_name: str) -> str:
    """
    Get an API key from config or environment.

    Args:
        config_value: API key from config (optional)
        env_var: Name of the environment variable fallback
        backend_name: Human-readable backend name for error messages

    Returns:
        API key string

    Raises:
        ValueError: If API key cannot be found
    """
    api_key = (
        config_value.get_secret_value() if config_value is not None else os.environ.get(env_var)
    )
    if not api_key:
        raise ValueError(
            f"{backend_name} API key is required. Provide it in the config or set the "
            f"{env_var} environment variable."
        )
    return api_key


def resolve_dockerfile(dockerfile_path: str | None, local_project_root: Path) -> Path:
    """
    Resolve and validate the Dockerfile path for a backend config.

    Args:
        dockerfile_path: Explicit path from config (absolute or relative to project root),
            or None to default to `Dockerfile` in the project root
        local_project_root: Path to the local project root directory

    Returns:
        Resolved Dockerfile path

    Raises:
        ValueError: If the Dockerfile doesn't exist or isn't a file
    """
    project_root_path = local_project_root.resolve()

    if dockerfile_path:
        dockerfile = Path(dockerfile_path)
        if not dockerfile.is_absolute():
            dockerfile = project_root_path / dockerfile
    else:
        dockerfile = project_root_path / "Dockerfile"

    if not dockerfile.exists():
        raise ValueError(
            f"Dockerfile not found at path: {dockerfile}. "
            "Specify dockerfile_path in config or ensure Dockerfile exists in project root."
        )
    if not dockerfile.is_file():
        raise ValueError(f"Dockerfile path is not a file: {dockerfile}")

    return dockerfile.resolve()


# Never part of the build-context hash, even without a .dockerignore. These either
# can't meaningfully be COPY'd into an image (VCS internals, virtualenvs — venv
# paths don't survive relocation) or churn constantly without affecting the build
# (caches): hashing them would make every `git fetch` or test run look like a
# source change and trigger a spurious rebuild.
_ALWAYS_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
    }
)
_ALWAYS_EXCLUDED_FILES = frozenset({".DS_Store"})


def _parse_dockerignore(root: Path) -> list[tuple[bool, tuple[str, ...]]]:
    """Parse `.dockerignore` into ordered (negated, pattern_segments) rules."""
    ignore_file = root / ".dockerignore"
    if not ignore_file.is_file():
        return []

    rules: list[tuple[bool, tuple[str, ...]]] = []
    for raw_line in ignore_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        # Docker cleans patterns (Go filepath.Clean) and matches them relative to
        # the context root, so leading and trailing '/' carry no meaning.
        cleaned = posixpath.normpath(line.strip("/"))
        if cleaned in ("", "."):
            continue
        rules.append((negated, tuple(cleaned.split("/"))))
    return rules


def _segments_match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """Match path segments against pattern segments; `**` spans any number of segments."""
    if not pattern:
        return not path
    if pattern[0] == "**":
        return any(_segments_match(pattern[1:], path[i:]) for i in range(len(path) + 1))
    return (
        bool(path)
        and fnmatch.fnmatchcase(path[0], pattern[0])
        and _segments_match(pattern[1:], path[1:])
    )


def _rule_matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    # A pattern that matches a directory applies to everything beneath it, so
    # test the full path and every ancestor prefix.
    return any(_segments_match(pattern, path[:i]) for i in range(1, len(path) + 1))


def _is_ignored(path: tuple[str, ...], rules: list[tuple[bool, tuple[str, ...]]]) -> bool:
    # Docker semantics: rules apply in file order, last matching rule wins.
    ignored = False
    for negated, pattern in rules:
        if _rule_matches(pattern, path):
            ignored = not negated
    return ignored


@cache
def _context_hash(root: Path, dockerfile: Path) -> str:
    """
    Content hash of the build context (honoring `.dockerignore`) plus the Dockerfile.

    Cached for the lifetime of the process: edits made after the first sandbox
    acquisition in a process are picked up on the next process start.
    """
    rules = _parse_dockerignore(root)
    # An ignored directory can be pruned from the walk only if no negated rule
    # exists that might re-include a file beneath it.
    has_negations = any(negated for negated, _ in rules)

    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_parts = Path(dirpath).relative_to(root).parts
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _ALWAYS_EXCLUDED_DIRS
            and (has_negations or not _is_ignored(rel_parts + (d,), rules))
        )
        for name in filenames:
            path_segments = rel_parts + (name,)
            if name in _ALWAYS_EXCLUDED_FILES or _is_ignored(path_segments, rules):
                continue
            entries.append(("/".join(path_segments), Path(dirpath) / name))

    digest = hashlib.sha256()
    for rel_name, file_path in sorted(entries):
        if not file_path.is_file():  # broken symlinks, sockets, etc.
            continue
        digest.update(rel_name.encode())
        digest.update(b"\0")
        with open(file_path, "rb") as f:
            digest.update(hashlib.file_digest(f, "sha256").digest())
    # The Dockerfile may live outside the context root (dockerfile_path accepts
    # absolute paths) and .dockerignore may even list it, but its content always
    # shapes the image — hash it explicitly so edits always invalidate.
    digest.update(dockerfile.read_bytes())
    return digest.hexdigest()[:8]


def image_name(
    *,
    prefix: str,
    dockerfile: Path,
    local_project_root: Path,
) -> str:
    """
    Compute the full image (template/snapshot) name for a backend config.

    Format: `{prefix}-{context_hash}`. The hash covers every file in the project
    root that `.dockerignore` doesn't exclude, plus the Dockerfile itself, so
    editing anything the image could contain automatically produces a new name —
    no version bumping required.

    Args:
        prefix: Name prefix from config
        dockerfile: Resolved Dockerfile path (see resolve_dockerfile)
        local_project_root: Path to the local project root directory

    Returns:
        Image name string
    """
    return f"{prefix}-{_context_hash(local_project_root.resolve(), dockerfile)}"
