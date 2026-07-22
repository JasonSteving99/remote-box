"""Remote Box — Type-safe remote Python function execution.

A decorator-based framework for executing Python functions in remote environments
with type-safe inputs and outputs, including reusable sandbox sessions.
"""

from remote.decorator import remote, RemoteFunction
from remote.session import RemoteSession, current_session
from remote.runtime import build_all, in_remote_execution
from remote.backends import (
    BackendType,
    BackendConfig,
    Subprocess,
    E2B,
    Daytona,
    AnyBackendConfig,
    MissingImageError,
    PauseSemantics,
    RemoteExecutionError,
    RemoteExecutionErrorResponse,
    RemoteExecutionProtocolError,
    SessionRef,
)

__all__ = [
    # Main decorator
    "remote",
    "RemoteFunction",
    # Sandbox sessions
    "RemoteSession",
    "SessionRef",
    "PauseSemantics",
    "current_session",
    # Explicit image building (CI/CD)
    "build_all",
    # Composing other decorators with @remote
    "in_remote_execution",
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
