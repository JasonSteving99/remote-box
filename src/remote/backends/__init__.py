from enum import Enum, auto
from pydantic import BaseModel, Field
from typing import Literal, Protocol, Any, Coroutine


class BackendType(Enum):
    """Backend type identifiers."""

    SUBPROCESS = auto()
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


class Subprocess(BackendConfig[Literal[BackendType.SUBPROCESS]]):
    """Configuration for subprocess backend execution."""

    type: Literal[BackendType.SUBPROCESS] = BackendType.SUBPROCESS
    shell: BackendShell = Field(..., description="Shell to use for execution harness")


# Type alias for all backend configs (discriminated union)
# Add new backend configs here as they're implemented
AnyBackendConfig = Subprocess


class Backend(Protocol):
    """Protocol that all backend implementations must follow."""

    @staticmethod
    def pre_check(config: AnyBackendConfig) -> None:
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

        Raises:
            Exception: If pre-checks fail (e.g., missing API key, invalid config)
        """
        ...

    @staticmethod
    async def execute[O: BaseModel](
        bash_script: str,
        output_model_class: type[O],
        timeout_millis: int,
    ) -> O:
        """
        Execute the remote function and return the parsed result.

        Args:
            bash_script: Fully formatted bash script to execute (including execution harness)
            output_model_class: The Pydantic model class to parse the output
            timeout_millis: Maximum time to wait for execution in milliseconds

        Returns:
            Parsed output model instance

        Raises:
            TimeoutError: If execution exceeds timeout_millis
            Exception: If execution fails
        """
        ...
