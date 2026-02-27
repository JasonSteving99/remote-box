from typing import Any, Callable, Coroutine
from pydantic import BaseModel, ValidationError
import inspect
from pathlib import Path
import os
import functools

from remote.backends import (
    Subprocess,
    BackendShell,
    BackendType,
    SHELL_EXECUTABLES,
    AnyBackendConfig,
    Backend,
    RemoteExecutionErrorResponse,
    RemoteExecutionError,
)
from remote.backends.subprocess import SubprocessBackend
from remote.backends.e2b import E2BBackend
from remote.backends.daytona import DaytonaBackend

# Load the execution harness template
_HARNESS_TEMPLATE_PATH = Path(__file__).parent / "execution_harness.sh.tmpl"
_HARNESS_TEMPLATE = _HARNESS_TEMPLATE_PATH.read_text()

EXECUTION_TEMPLATE = """
import asyncio
import json
import os
import sys

def _write_result(result_file: str, data: str):
    with open(result_file, 'w') as f:
        print(data, file=f)

async def execute():
    result_file = os.environ.get('REMOTE_EXECUTION_RESULT_FILE')
    if not result_file:
        print("Error: REMOTE_EXECUTION_RESULT_FILE environment variable not set", file=sys.stderr)
        sys.exit(1)

    try:
        {import_model}
        {import_func}
        res = await {func_name}({arg})
        _write_result(result_file, res.model_dump_json())
    except Exception as e:
        error_response = json.dumps({{
            "__remote_execution_error__": True,
            "error_type": type(e).__name__,
            "error_message": str(e),
        }})
        _write_result(result_file, error_response)
        print(f"Remote execution failed: {{e}}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(execute())

"""

# Registry mapping backend types to their implementations
_BACKEND_REGISTRY: dict[BackendType, type[Backend]] = {
    BackendType.SUBPROCESS: SubprocessBackend,
    BackendType.E2B: E2BBackend,
    BackendType.DAYTONA: DaytonaBackend,
}

# Cache to track which backend configurations have been pre-checked
# Uses list since Pydantic models aren't hashable but support equality comparison.
# Checking such a short list is likely just as (or more) efficient than checking a set anyways.
_PRECHECKED_CONFIGS: list[AnyBackendConfig] = []


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
    # Get the backend implementation from the registry
    backend_impl = _BACKEND_REGISTRY[backend.type]

    # Run pre-checks when the decorator is first applied (at import time)
    # Skip pre-checks if we're already in remote execution mode
    # Only run once per unique backend configuration for performance
    if os.environ.get("REMOTE_EXECUTION_MODE") != "1" and backend not in _PRECHECKED_CONFIGS:
        backend_impl.pre_check(backend, local_project_root)
        _PRECHECKED_CONFIGS.append(backend)

    def decorator(
        func: Callable[[I], Coroutine[Any, Any, O]],
    ) -> Callable[[I], Coroutine[Any, Any, O]]:
        # Extract the actual output model class from the return type annotation
        # For async functions, the annotation is the output type directly (not wrapped in Coroutine)
        output_model_class = inspect.get_annotations(func)["return"]

        backend_shell = SHELL_EXECUTABLES[backend.shell]

        @functools.wraps(func)
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
            bash_script = _HARNESS_TEMPLATE.format(shell=backend_shell, python_cmd=backend_impl.PYTHON_CMD, code=python_code)

            # Execute using the configured backend with timeout
            stdout_str = await backend_impl.execute(
                backend, local_project_root, bash_script, timeout_millis
            )

            # Parse the response, trying happy path first
            try:
                return output_model_class.model_validate_json(stdout_str)
            except ValidationError:
                # If that fails, try parsing as error response
                error_response = RemoteExecutionErrorResponse.model_validate_json(stdout_str)
                raise RemoteExecutionError(error_response)

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
