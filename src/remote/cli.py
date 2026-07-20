"""remote-box CLI.

`remote-box build` imports the given modules (which registers every @remote
decorator's backend target as a side effect of the import) and then builds any
missing backend images. Run it in CI/CD so production deployments can set
REMOTE_BOX_AUTO_BUILD=false and never pay the build cost — or risk — at runtime.

Targets may be dotted module names, .py files, or directories (crawled
recursively for .py files, skipping hidden/venv/cache directories).
"""

import argparse
import importlib
import importlib.util
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from remote.runtime import TargetResult, build_all, check_all

# Directory names that never contain user task modules worth importing.
_SKIP_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    ".venv",
    "venv",
    ".tox",
    ".eggs",
}


def _iter_python_files(directory: Path) -> list[Path]:
    """Recursively find .py files, skipping hidden and dependency/cache directories."""
    found: list[Path] = []
    for path in sorted(directory.rglob("*.py")):
        relative_parts = path.relative_to(directory).parts
        if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in relative_parts):
            continue
        found.append(path)
    return found


def _dotted_module_name(path: Path) -> str | None:
    """Compute the importable dotted module name for a file, relative to cwd.

    Importing by proper package path (rather than as a standalone file) keeps
    relative imports and package __init__ semantics working. Returns None when
    the file doesn't form a valid package path under the cwd.
    """
    try:
        rel = path.resolve().relative_to(Path.cwd())
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and all(part.isidentifier() for part in parts):
        return ".".join(parts)
    return None


def _import_file(path: Path) -> None:
    """Import a single .py file, preferring its package path when it has one."""
    # A script's own directory is on sys.path when run directly, and sandboxes
    # import task modules with the project root as cwd — mirror that here so
    # sibling imports (`from helper import X`) resolve during discovery too.
    parent = str(path.parent.resolve())
    if parent not in sys.path:
        sys.path.insert(0, parent)

    dotted = _dotted_module_name(path)
    if dotted is not None:
        importlib.import_module(dotted)
        return

    # Standalone fallback for files outside the cwd package tree. Unique module
    # name derived from the full path so same-named files can't collide.
    module_name = re.sub(r"\W", "_", str(path.resolve().with_suffix("")))
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


def _import_target(target: str) -> list[tuple[Path, Exception]]:
    """Import a build target (dotted module, .py file, or directory to crawl).

    Returns per-file import failures instead of raising, so one broken file
    doesn't hide the rest of a crawled directory.
    """
    as_path = Path(target)
    if as_path.is_dir():
        failures: list[tuple[Path, Exception]] = []
        files = _iter_python_files(as_path)
        if not files:
            raise FileNotFoundError(f"No Python files found under directory: {as_path}")
        for file in files:
            try:
                _import_file(file)
            except Exception as e:  # noqa: BLE001 — reported to the user, not swallowed
                failures.append((file, e))
        return failures

    if target.endswith(".py"):
        if not as_path.exists():
            raise FileNotFoundError(f"Module file not found: {as_path}")
        _import_file(as_path)
        return []

    importlib.import_module(target)
    return []


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render an aligned plain-text table with a separator under the header."""
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    lines = [
        "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    return "\n".join(lines)


def _print_results(results: list[TargetResult]) -> None:
    """Print target results as a table, with project roots factored out when shared."""
    shared_root = len({r.project_root for r in results}) == 1

    headers = ["BACKEND", "IMAGE", "STATUS"]
    rows = [
        [r.backend, r.image if r.image is not None else "(local)", r.status] for r in results
    ]
    if not shared_root:
        headers.append("PROJECT ROOT")
        for row, result in zip(rows, results):
            row.append(str(result.project_root))
    if any(r.detail for r in results):
        headers.append("DETAIL")
        for row, result in zip(rows, results):
            row.append(result.detail)

    print()
    print(_format_table(headers, rows))
    if shared_root:
        print(f"\nProject root: {results[0].project_root}")

    counts = {status: sum(1 for r in results if r.status == status) for status in
              dict.fromkeys(r.status for r in results)}
    summary = ", ".join(f"{count} {status}" for status, count in counts.items())
    print(f"{len(results)} target(s): {summary}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="remote-box",
        description="Type-safe remote Python function execution framework.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build",
        help="Build backend images (E2B templates / Daytona snapshots) for all "
        "@remote-decorated functions in the given targets.",
    )
    build_parser.add_argument(
        "targets",
        nargs="+",
        help="Where to find @remote-decorated functions: dotted module names "
        "(myproject.tasks), .py files (src/tasks.py), or directories to crawl "
        "recursively (src/).",
    )
    build_parser.add_argument(
        "--env-file",
        default=None,
        help="Path to an env file to load (e.g. .env.local). "
        "Defaults to auto-detecting a .env file.",
    )
    build_parser.add_argument(
        "--check",
        action="store_true",
        help="Discovery only: report each discovered target and whether its image "
        "already exists, without building anything. Exits 0 only if everything "
        "is ready — usable in CI to verify images exist before deploying.",
    )

    args = parser.parse_args(argv)

    if args.env_file:
        env_path = Path(args.env_file)
        if not env_path.exists():
            print(f"Error: env file not found: {env_path}", file=sys.stderr)
            return 1
        load_dotenv(env_path)
    else:
        load_dotenv()

    # Importing user modules must resolve the same way as `python -m` from the cwd.
    sys.path.insert(0, str(Path.cwd()))

    import_failures: list[tuple[Path, Exception]] = []
    for target in args.targets:
        print(f"Importing {target}...")
        import_failures.extend(_import_target(target))

    results = check_all() if args.check else build_all()
    if results:
        _print_results(results)
    else:
        print("No @remote-decorated functions were registered by the given targets.")

    # Build mode succeeds on "built" or "ready"; check mode only on "ready".
    ok_statuses = {"ready"} if args.check else {"ready", "built"}
    all_ok = bool(results) and all(r.status in ok_statuses for r in results)

    if import_failures:
        # Fail loudly: a file that doesn't import may contain @remote functions
        # whose images silently wouldn't get built — CI must not pass on that.
        print(f"\nERROR: {len(import_failures)} file(s) failed to import:", file=sys.stderr)
        for file, error in import_failures:
            print(f"  - {file}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
