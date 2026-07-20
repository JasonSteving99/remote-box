"""Remote Box — Type-safe remote Python function execution.

A decorator-based framework for executing Python functions in remote environments
with type-safe inputs and outputs, including reusable sandbox sessions.
"""

from remote.decorator import remote, RemoteFunction
from remote.session import RemoteSession
from remote.runtime import build_all
from remote.backends import (
    BackendType,
    BackendConfig,
    Subprocess,
    E2B,
    Daytona,
    AnyBackendConfig,
    MissingImageError,
    RemoteExecutionError,
    RemoteExecutionErrorResponse,
    RemoteExecutionProtocolError,
)

__all__ = [
    # Main decorator
    "remote",
    "RemoteFunction",
    # Sandbox sessions
    "RemoteSession",
    # Explicit image building (CI/CD)
    "build_all",
    # Backend types and configs
    "BackendType",
    "BackendConfig",
    "Subprocess",
    "E2B",
    "Daytona",
    "AnyBackendConfig",
    # Exceptions
    "MissingImageError",
    "RemoteExecutionError",
    "RemoteExecutionErrorResponse",
    "RemoteExecutionProtocolError",
]
