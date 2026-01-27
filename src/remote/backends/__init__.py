from enum import Enum, auto
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal, Optional, Protocol, Any, Coroutine


class BackendType(Enum):
    """Backend type identifiers."""

    SUBPROCESS = auto()
    E2B = auto()
    # Future: UBUNTU = "ubuntu", SSH = "ssh", etc.


class BackendShell(Enum):
    """Available shells for execution harness."""

    BASH4 = auto()  # Bash 4+ (common on Linux/Ubuntu)
    ZSH = auto()  # Zsh (common on macOS)


# Map BackendShell enum to actual shell executable names
SHELL_EXECUTABLES = {
    BackendShell.BASH4: "bash",
    BackendShell.ZSH: "zsh",
}


class BackendConfig[T: BackendType](BaseModel):
    """Base class for backend configurations."""

    type: T = Field(..., description="Backend type discriminator")
    shell: BackendShell = Field(..., description="Shell to use for execution harness")


class Subprocess(BackendConfig[Literal[BackendType.SUBPROCESS]]):
    """Configuration for subprocess backend execution."""

    type: Literal[BackendType.SUBPROCESS] = BackendType.SUBPROCESS
    shell: BackendShell = Field(..., description="Shell to use for execution harness")


class E2B(BackendConfig[Literal[BackendType.E2B]]):
    """Configuration for E2B backend execution."""

    type: Literal[BackendType.E2B] = BackendType.E2B
    shell: BackendShell = BackendShell.BASH4
    e2b_api_key: Optional[str] = Field(
        default=None,
        description="API key for E2B backend. If this isn't set, it must be provided via environment variable `E2B_API_KEY`.",
    )
    template_prefix: str = Field(
        ...,
        description="Prefix for E2B template naming. Full template alias will be '{prefix}-v{version}'.",
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


# Type alias for all backend configs (discriminated union)
# Add new backend configs here as they're implemented
AnyBackendConfig = Subprocess | E2B


class RemoteExecutionErrorResponse(BaseModel):
    """Response model for remote execution errors."""

    __remote_execution_error__: Literal[True]
    error_type: str
    error_message: str
    # traceback: str


class RemoteExecutionError(Exception):
    """Exception raised when remote execution fails with an error."""

    def __init__(self, error_response: RemoteExecutionErrorResponse):
        self.error_type = error_response.error_type
        self.error_message = error_response.error_message
        # self.remote_traceback = error_response.traceback
        super().__init__(
            f"Remote execution failed with {self.error_type}: {self.error_message}\n"
            # f"Remote traceback:\n{self.remote_traceback}"
        )


class Backend(Protocol):
    """Protocol that all backend implementations must follow."""

    @staticmethod
    def pre_check(config: AnyBackendConfig, local_project_root: Path) -> None:
        """
        Run pre-checks when the decorator is first applied.

        This is called exactly once per unique backend configuration (cached across all
        decorator invocations). It runs when the decorator is evaluated (at file import time),
        not when the decorated function is called. Use this to verify environment setup,
        check for required dependencies, validate API keys, etc.

        Note: Results are cached, so this will only execute once per unique config value.
        For example, if you use Subprocess(shell=BackendShell.ZSH) in 10 different
        decorators, this pre-check will only run once for that configuration.

        Args:
            config: The backend configuration
            local_project_root: Path to the local project root directory

        Raises:
            Exception: If pre-checks fail (e.g., missing API key, invalid config)
        """
        ...

    @staticmethod
    async def execute(
        config: AnyBackendConfig,
        local_project_root: Path,
        bash_script: str,
        timeout_millis: int,
    ) -> str:
        """
        Execute the remote function and return the raw stdout.

        Args:
            config: The backend configuration
            local_project_root: Path to the local project root directory
            bash_script: Fully formatted bash script to execute (including execution harness)
            timeout_millis: Maximum time to wait for execution in milliseconds

        Returns:
            Raw stdout from execution as string

        Raises:
            TimeoutError: If execution exceeds timeout_millis
            Exception: If execution fails
        """
        ...
