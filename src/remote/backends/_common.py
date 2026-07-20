"""Helpers shared by the cloud sandbox backends (E2B, Daytona)."""

import hashlib
import os
import tomllib
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


@cache
def _project_version(local_project_root: Path) -> str:
    pyproject_path = local_project_root.resolve() / "pyproject.toml"
    if not pyproject_path.exists():
        raise ValueError(
            f"pyproject.toml not found in project root: {local_project_root.resolve()}. "
            "This backend requires a project version for image naming."
        )

    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)

    version = pyproject_data.get("project", {}).get("version")
    if not version:
        raise ValueError(
            "Project version not found in pyproject.toml. "
            "This backend requires a version for image naming."
        )
    return version


@cache
def _dockerfile_hash(dockerfile: Path) -> str:
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:8]


def image_name(
    *,
    prefix: str,
    version: str | None,
    dockerfile: Path,
    local_project_root: Path,
) -> str:
    """
    Compute the full image (template/snapshot) name for a backend config.

    Format: `{prefix}-v{version}-{dockerfile_hash}`. The Dockerfile content hash is
    included so editing the Dockerfile invalidates the cached image even without a
    version bump. Note this does NOT detect changes to source files COPY'd by the
    Dockerfile — bump the version (or delete the image) after source-only changes.

    Args:
        prefix: Name prefix from config
        version: Explicit version from config, or None to read pyproject.toml
        dockerfile: Resolved Dockerfile path (see resolve_dockerfile)
        local_project_root: Path to the local project root directory

    Returns:
        Image name string
    """
    resolved_version = version or _project_version(local_project_root)
    return f"{prefix}-v{resolved_version}-{_dockerfile_hash(dockerfile)}"
