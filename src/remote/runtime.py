"""Shared runtime machinery: backend registry, harness formatting, and build orchestration."""

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from remote.backends import AnyBackendConfig, Backend, BackendType, MissingImageError
from remote.backends.subprocess import SubprocessBackend
from remote.backends.e2b import E2BBackend
from remote.backends.daytona import DaytonaBackend

# Load the execution harness template
_HARNESS_TEMPLATE_PATH = Path(__file__).parent / "execution_harness.sh.tmpl"
_HARNESS_TEMPLATE = _HARNESS_TEMPLATE_PATH.read_text()

# Registry mapping backend types to their implementations
BACKEND_REGISTRY: dict[BackendType, type[Backend]] = {
    BackendType.SUBPROCESS: SubprocessBackend,
    BackendType.E2B: E2BBackend,
    BackendType.DAYTONA: DaytonaBackend,
}

# Every (config, project_root) pair seen by the @remote decorator. Consumed by
# build_all() / the `remote-box build` CLI to know what images to build.
# Uses lists since Pydantic models aren't hashable but support equality comparison;
# these lists are tiny so linear scans are fine.
_REGISTERED_TARGETS: list[tuple[AnyBackendConfig, Path]] = []

# (config, root) pairs whose ensure_built has already succeeded in this process.
_ENSURED: list[tuple[AnyBackendConfig, Path]] = []
_ENSURE_LOCK = threading.Lock()


AUTO_BUILD_ENV_VAR = "REMOTE_BOX_AUTO_BUILD"

_ENV_TRUE = ("1", "true", "yes", "on")
_ENV_FALSE = ("0", "false", "no", "off")

# Set by the execution harness (execution_harness.sh.tmpl) inside the sandbox only.
REMOTE_EXECUTION_MODE_ENV_VAR = "REMOTE_EXECUTION_MODE"


def in_remote_execution() -> bool:
    """
    True only inside the sandbox process spawned to run a `@remote`-decorated call.

    `@remote` itself uses this to skip straight to the wrapped function instead
    of recursing into another sandbox dispatch. It's public so any decorator
    meant to stack with `@remote` — e.g. an AI agent framework's `@tool(...)`
    that gates execution on human approval — can do the same: the sandbox
    re-imports the target module from scratch, which re-runs every decorator
    on the stack, not just `@remote`'s. Without this check, host-side gating
    logic (approval prompts, rate limits, logging) fires again remotely, where
    it can't actually reach a human and shouldn't re-run anyway.

    Recommended pattern for a composable decorator:

        def tool(sandboxed: bool = False):
            def decorator(func):
                @functools.wraps(func)
                async def wrapper(arg):
                    if in_remote_execution():
                        return await func(arg)  # host-side gating already ran
                    if sandboxed:
                        await request_human_approval(arg)
                    return await func(arg)
                return wrapper
            return decorator

    Stacking order with `@remote` doesn't matter as long as every decorator in
    the chain does this check: whichever one ends up outermost after the
    sandbox's fresh re-import sees the flag first and falls straight through
    to the next layer, cascading down to the real body with no logic re-run.
    """
    return os.environ.get(REMOTE_EXECUTION_MODE_ENV_VAR) == "1"


def resolve_auto_build(config: AnyBackendConfig) -> bool:
    """
    Decide whether a missing backend image may be built at runtime.

    Precedence: explicit `auto_build_override` on the config, then the
    REMOTE_BOX_AUTO_BUILD environment variable, then True. Keeping the override
    unset everywhere means the local-dev/production split is purely an environment
    concern — set REMOTE_BOX_AUTO_BUILD=false in production deployments and no
    source changes are needed to switch between the two.

    Raises:
        ValueError: If REMOTE_BOX_AUTO_BUILD is set to an unrecognized value
    """
    configured: bool | None = getattr(config, "auto_build_override", None)
    if configured is not None:
        return configured

    env_value = os.environ.get(AUTO_BUILD_ENV_VAR)
    if env_value is None:
        return True
    normalized = env_value.strip().lower()
    if normalized in _ENV_TRUE:
        return True
    if normalized in _ENV_FALSE:
        return False
    raise ValueError(
        f"Unrecognized value for {AUTO_BUILD_ENV_VAR}: {env_value!r}. "
        f"Expected one of {_ENV_TRUE + _ENV_FALSE}."
    )


def format_harness(python_cmd: str, python_code: str) -> str:
    """Wrap generated Python code in the bash execution harness."""
    return _HARNESS_TEMPLATE.format(python_cmd=python_cmd, code=python_code)


def register_target(config: AnyBackendConfig, local_project_root: Path) -> None:
    """Record a backend target so `remote-box build` / build_all() can find it."""
    target = (config, local_project_root)
    if target not in _REGISTERED_TARGETS:
        _REGISTERED_TARGETS.append(target)


def ensure_built_once(
    config: AnyBackendConfig, local_project_root: Path, *, allow_build: bool
) -> bool:
    """
    Run the backend's ensure_built exactly once per (config, project_root) pair.

    Thread-safe and cached for the lifetime of the process. Blocking (image builds
    can take minutes) — call via asyncio.to_thread from async code.

    Returns:
        True if a build was performed, False if the image already existed
        (or the result was cached from an earlier call).
    """
    key = (config, local_project_root)
    with _ENSURE_LOCK:
        if key in _ENSURED:
            return False
        built = BACKEND_REGISTRY[config.type].ensure_built(
            config, local_project_root, allow_build=allow_build
        )
        _ENSURED.append(key)
        return bool(built)


@dataclass
class TargetResult:
    """Outcome of building or checking one registered backend target."""

    backend: str  # e.g. "daytona"
    image: str | None  # resolved image name; None for imageless backends (subprocess)
    project_root: Path
    # build_all: "built" (build performed) | "ready" (already existed) | "error"
    # check_all: "ready" | "would build" (image missing) | "error"
    status: str
    detail: str = ""


def _resolve_image_name(config: AnyBackendConfig, root: Path) -> str | None:
    """Best-effort image name for reporting — never raises."""
    try:
        return BACKEND_REGISTRY[config.type].image_name(config, root)
    except Exception:
        return "?"


def build_all() -> list[TargetResult]:
    """
    Build/verify backend images for every registered @remote target.

    Intended for CI/CD (via the `remote-box build` CLI) or an explicit setup script.
    Builds missing images regardless of the auto-build setting (env var or override).
    One target failing doesn't stop the rest — failures are reported as "error"
    results so callers can surface all of them at once.
    """
    results: list[TargetResult] = []
    for config, root in _REGISTERED_TARGETS:
        backend = config.type.name.lower()
        image = _resolve_image_name(config, root)
        try:
            built = ensure_built_once(config, root, allow_build=True)
            results.append(
                TargetResult(backend, image, root, "built" if built else "ready")
            )
        except Exception as e:
            results.append(
                TargetResult(backend, image, root, "error", f"{type(e).__name__}: {e}")
            )
    return results


def check_all() -> list[TargetResult]:
    """
    Report what build_all() would do for every registered @remote target,
    without building anything.
    """
    results: list[TargetResult] = []
    for config, root in _REGISTERED_TARGETS:
        backend = config.type.name.lower()
        image = _resolve_image_name(config, root)
        try:
            BACKEND_REGISTRY[config.type].ensure_built(config, root, allow_build=False)
            results.append(TargetResult(backend, image, root, "ready"))
        except MissingImageError as e:
            # The error names the exact missing image, which beats a best-effort guess
            results.append(TargetResult(backend, e.image_name, root, "would build"))
        except Exception as e:
            results.append(
                TargetResult(backend, image, root, "error", f"{type(e).__name__}: {e}")
            )
    return results
