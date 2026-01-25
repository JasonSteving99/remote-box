from typing import Any, Callable, Coroutine
from pydantic import BaseModel
import inspect
from pathlib import Path
import os

from remote.backends import (
    Subprocess,
    BackendShell,
    BackendType,
    SHELL_EXECUTABLES,
    AnyBackendConfig,
)
from remote.backends import subprocess as subprocess_backend

# Load the execution harness template
_HARNESS_TEMPLATE_PATH = Path(__file__).parent / "execution_harness.sh.tmpl"
_HARNESS_TEMPLATE = _HARNESS_TEMPLATE_PATH.read_text()

EXECUTION_TEMPLATE = """
{import_model}
{import_func}

import asyncio
import os
import sys

async def execute():
    res = await {func_name}({arg})

    # Get the IPC FD from environment variable (required)
    ipc_fd_str = os.environ.get('REMOTE_EXECUTION_IPC_FD')
    if not ipc_fd_str:
        print("Error: REMOTE_EXECUTION_IPC_FD environment variable not set", file=sys.stderr)
        sys.exit(1)

    try:
        ipc_fd = int(ipc_fd_str)
        # Write to the IPC FD instead of stdout to avoid collisions with any logging the user code might do.
        with os.fdopen(ipc_fd, 'w') as f:
            print(res.model_dump_json(), file=f)
    except (OSError, ValueError) as e:
        print(f"Error: Failed to write to IPC FD {{ipc_fd_str}}: {{e}}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(execute())

"""

# Map backend type to execution functions
_BACKEND_EXECUTORS = {
    BackendType.SUBPROCESS: subprocess_backend.execute,
}


def remote[I: BaseModel, O: BaseModel](
    local_project_root: Path,
    backend: AnyBackendConfig = Subprocess(shell=BackendShell.ZSH),
    timeout_millis: int = 300000,  # 5 minutes default
) -> Callable[[Callable[[I], Coroutine[Any, Any, O]]], Callable[[I], Coroutine[Any, Any, O]]]:
    """
    Decorator that executes a function remotely using the specified backend.

    The decorated function must:
    - Take exactly one parameter of type BaseModel (or subclass)
    - Return a BaseModel (or subclass)

    Args:
        local_project_root: Root directory of the project for resolving imports
        backend: Backend configuration (default: Subprocess with ZSH for macOS compatibility)
        timeout_millis: Maximum execution time in milliseconds (default: 300000 = 5 minutes)

    Usage:
        class InputModel(BaseModel):
            name: str

        class OutputModel(BaseModel):
            greeting: str

        @remote(
            local_project_root=Path(__file__).parent,
            backend=Subprocess(shell=BackendShell.BASH4),
            timeout_millis=60000
        )
        async def my_function(input: InputModel) -> OutputModel:
            return OutputModel(greeting=f"Hello {input.name}")
    """

    def decorator(
        func: Callable[[I], Coroutine[Any, Any, O]],
    ) -> Callable[[I], Coroutine[Any, Any, O]]:
        # Extract the actual output model class from the return type annotation
        # For async functions, the annotation is the output type directly (not wrapped in Coroutine)
        output_model_class = inspect.get_annotations(func)["return"]

        # Get the backend executor function based on backend type
        backend_executor = _BACKEND_EXECUTORS[backend.type]
        backend_shell = SHELL_EXECUTABLES[backend.shell]

        async def wrapper(arg: I) -> O:
            # If we're already in remote execution mode, just call the function directly
            if os.environ.get("REMOTE_EXECUTION_MODE") == "1":
                return await func(arg)

            # Generate the Python code with the actual argument
            python_code = EXECUTION_TEMPLATE.format(
                import_model=__get_import_path(type(arg), local_project_root),
                import_func=__get_import_path(func, local_project_root),
                func_name=func.__name__,
                arg=arg.__repr__(),
            )

            # Wrap the Python code in the execution harness bash script
            bash_script = _HARNESS_TEMPLATE.format(shell=backend_shell, code=python_code)

            # Execute using the configured backend with timeout
            return await backend_executor(bash_script, output_model_class, timeout_millis)

        return wrapper

    return decorator


def __get_import_path(obj, local_project_root: Path) -> str:
    """Get import string for a function or class.

    Args:
        obj: Function or class to get import string for
        local_project_root: Root directory of the project (required for __main__ modules)
    """
    module = obj.__module__

    if module == "__main__":
        source_file = inspect.getsourcefile(obj)
        if not source_file:
            raise ValueError(f"Cannot determine source file for {obj}")

        source_path = Path(source_file).resolve()
        relative = source_path.relative_to(local_project_root.resolve())
        # Convert path to module notation
        module = str(relative.with_suffix("")).replace("/", ".").replace("\\", ".")

    return f"from {module} import {obj.__name__}"
