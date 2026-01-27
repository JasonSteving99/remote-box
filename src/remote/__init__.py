"""SuperComputer Remote Execution Framework

A decorator-based framework for executing Python functions in remote environments
with type-safe inputs and outputs.
"""

from remote.decorator import remote
from remote.backends import (
    BackendType,
    BackendShell,
    BackendConfig,
    Subprocess,
    E2B,
    AnyBackendConfig,
    RemoteExecutionError,
    RemoteExecutionErrorResponse,
)

__all__ = [
    # Main decorator
    "remote",
    # Backend types and configs
    "BackendType",
    "BackendShell",
    "BackendConfig",
    "Subprocess",
    "E2B",
    "AnyBackendConfig",
    # Exceptions
    "RemoteExecutionError",
    "RemoteExecutionErrorResponse",
]
