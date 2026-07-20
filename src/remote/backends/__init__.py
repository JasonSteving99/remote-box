from enum import Enum, auto
from pathlib import Path
from pydantic import BaseModel, Field, SecretStr
from typing import Any, Literal, Optional, Protocol


class BackendType(Enum):
    """Backend type identifiers."""

    SUBPROCESS = auto()
    E2B = auto()
    DAYTONA = auto()
    # Future: UBUNTU = "ubuntu", SSH = "ssh", etc.


class BackendConfig[T: BackendType](BaseModel):
    """Base class for backend configurations."""

    type: T = Field(..., description="Backend type discriminator")


class Subprocess(BackendConfig[Literal[BackendType.SUBPROCESS]]):
    """Configuration for subprocess backend execution."""

    type: Literal[BackendType.SUBPROCESS] = BackendType.SUBPROCESS


class E2B(BackendConfig[Literal[BackendType.E2B]]):
    """Configuration for E2B backend execution."""

    type: Literal[BackendType.E2B] = BackendType.E2B
    e2b_api_key: Optional[SecretStr] = Field(
        default=None,
        description="API key for E2B backend. If this isn't set, it must be provided via environment variable `E2B_API_KEY`.",
    )
    template_prefix: str = Field(
        ...,
        description="Prefix for E2B template naming. Full template alias will be '{prefix}-v{version}-{dockerfile_hash}'.",
    )
    template_version: Optional[str] = Field(
        default=None,
        description="Version to use for template naming. If not provided, reads project's pyproject.toml version.",
    )
    dockerfile_path: Optional[str] = Field(
        default=None,
        description="Path to Dockerfile for E2B backend, if any. If not provided will look for `Dockerfile` in local project root.",
    )
    cpu_count: int = Field(
        default=1,
        description="Number of CPUs to allocate for the E2B sandbox.",
    )
    memory_mb: int = Field(
        default=1024,
        description="Amount of memory in MB to allocate for the E2B sandbox.",
    )
    auto_build_override: Optional[bool] = Field(
        default=None,
        description=(
            "Per-config OVERRIDE of the auto-build behavior; leave unset (None) in almost "
            "all cases so behavior is controlled by the REMOTE_BOX_AUTO_BUILD environment "
            "variable (default True: build missing templates automatically on first use; "
            "False: raise MissingImageError directing you to `remote-box build`). Setting "
            "True/False here pins THIS config regardless of the environment — meaning source "
            "changes to flip between local dev and production, which defeats the env var."
        ),
    )
    sandbox_ttl_seconds: int = Field(
        default=600,
        description=(
            "Sandbox lifetime timeout in seconds. Refreshed before every call, so a session "
            "stays alive as long as calls keep arriving within this window."
        ),
    )


class Daytona(BackendConfig[Literal[BackendType.DAYTONA]]):
    """Configuration for Daytona backend execution."""

    type: Literal[BackendType.DAYTONA] = BackendType.DAYTONA
    daytona_api_key: Optional[SecretStr] = Field(
        default=None,
        description="API key for Daytona backend. If not set, must be provided via environment variable `DAYTONA_API_KEY`.",
    )
    snapshot_name: str = Field(
        ...,
        description="Prefix for Daytona snapshot naming. Full snapshot name will be '{snapshot_name}-v{version}-{dockerfile_hash}'.",
    )
    snapshot_version: Optional[str] = Field(
        default=None,
        description="Version to use for snapshot naming. If not provided, reads project's pyproject.toml version.",
    )
    dockerfile_path: Optional[str] = Field(
        default=None,
        description="Path to Dockerfile for Daytona backend. If not provided will look for `Dockerfile` in local project root.",
    )
    cpu_count: int = Field(
        default=1,
        description="Number of CPUs to allocate for the Daytona sandbox.",
    )
    memory_gb: int = Field(
        default=1,
        description="Amount of memory in GB to allocate for the Daytona sandbox.",
    )
    disk_gb: int = Field(
        default=3,
        description="Amount of disk space in GB to allocate for the Daytona sandbox.",
    )
    auto_build_override: Optional[bool] = Field(
        default=None,
        description=(
            "Per-config OVERRIDE of the auto-build behavior; leave unset (None) in almost "
            "all cases so behavior is controlled by the REMOTE_BOX_AUTO_BUILD environment "
            "variable (default True: build missing snapshots automatically on first use; "
            "False: raise MissingImageError directing you to `remote-box build`). Setting "
            "True/False here pins THIS config regardless of the environment — meaning source "
            "changes to flip between local dev and production, which defeats the env var."
        ),
    )
    create_timeout_seconds: float = Field(
        default=120,
        description="Maximum time in seconds to wait for sandbox creation.",
    )


# Type alias for all backend configs (discriminated union)
# Add new backend configs here as they're implemented
AnyBackendConfig = Subprocess | E2B | Daytona


class RemoteExecutionErrorResponse(BaseModel):
    """Response model for remote execution errors.

    The `remote_execution_error: True` sentinel distinguishes this payload from a
    successful result, so it must be a real (non-underscore) Pydantic field.
    """

    remote_execution_error: Literal[True]
    error_type: str
    error_message: str
    traceback: str


class RemoteExecutionError(Exception):
    """Exception raised when the remote function raised an error."""

    def __init__(self, error_response: RemoteExecutionErrorResponse):
        self.error_type = error_response.error_type
        self.error_message = error_response.error_message
        self.remote_traceback = error_response.traceback
        super().__init__(
            f"Remote execution failed with {self.error_type}: {self.error_message}\n"
            f"Remote traceback:\n{self.remote_traceback}"
        )


class RemoteExecutionProtocolError(Exception):
    """Raised when the remote response can't be parsed as a result or a structured error."""

    def __init__(self, raw_output: str):
        self.raw_output = raw_output
        super().__init__(
            "Could not parse remote response as either the declared output model or a "
            f"structured error response. Raw output:\n{raw_output[:2000]}"
        )


class MissingImageError(Exception):
    """Raised when auto-building is disabled and the backend image doesn't exist yet."""

    def __init__(self, image_name: str):
        self.image_name = image_name
        super().__init__(
            f"Backend image '{image_name}' does not exist and auto-building is disabled "
            "(via the REMOTE_BOX_AUTO_BUILD environment variable or the auto_build_override config field). "
            "Build it explicitly (e.g. in CI/CD) with: remote-box build <module-with-decorated-functions>"
        )


class Backend(Protocol):
    """Protocol that all backend implementations must follow.

    The lifecycle is split so a sandbox can be reused across calls:
    `acquire` once, `run` any number of scripts against the same sandbox
    (its filesystem persists between runs), then `release`.
    """

    PYTHON_CMD: str

    @staticmethod
    def ensure_built(
        config: AnyBackendConfig, local_project_root: Path, *, allow_build: bool
    ) -> bool:
        """
        Validate the environment and make sure the backend image exists.

        Called lazily before the first `acquire` for a given config (cached), and
        explicitly by `remote-box build` / `build_all()`. Synchronous because image
        builds are long-running blocking operations; async callers should wrap in
        `asyncio.to_thread`.

        Args:
            config: The backend configuration
            local_project_root: Path to the local project root directory
            allow_build: If True, build the image when missing. If False, raise
                MissingImageError instead (production mode — builds belong in CI/CD).

        Returns:
            True if a build was performed, False if the image already existed
            (or the backend has nothing to build).

        Raises:
            MissingImageError: If the image is missing and allow_build is False
            Exception: If validation fails (e.g., missing API key, missing Dockerfile)
        """
        ...

    @staticmethod
    def image_name(config: AnyBackendConfig, local_project_root: Path) -> Optional[str]:
        """
        Resolve the image (template/snapshot) name this config executes on.

        Returns None for backends with no image (e.g. local subprocess). Used for
        human-readable reporting; may raise if the name can't be resolved (missing
        Dockerfile/pyproject.toml).
        """
        ...

    @staticmethod
    async def acquire(
        config: AnyBackendConfig, local_project_root: Path, timeout_millis: int
    ) -> Any:
        """
        Create (or connect to) a sandbox and return an opaque handle for `run`/`release`.

        Args:
            config: The backend configuration
            local_project_root: Path to the local project root directory
            timeout_millis: Maximum time to wait for sandbox acquisition

        Returns:
            Backend-specific sandbox handle
        """
        ...

    @staticmethod
    async def run(handle: Any, bash_script: str, timeout_millis: int) -> str:
        """
        Execute a harness script in the sandbox and return its raw stdout.

        Args:
            handle: Sandbox handle returned by `acquire`
            bash_script: Fully formatted bash script to execute (including execution harness)
            timeout_millis: Maximum time to wait for execution in milliseconds

        Returns:
            Raw stdout from execution as string

        Raises:
            TimeoutError: If execution exceeds timeout_millis
            Exception: If execution fails
        """
        ...

    @staticmethod
    async def release(handle: Any) -> None:
        """
        Destroy the sandbox and free all resources associated with the handle.

        Must be safe to call even if `run` raised.
        """
        ...
