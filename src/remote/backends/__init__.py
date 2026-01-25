from enum import Enum, auto
from pydantic import BaseModel, Field
from typing import Literal


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
