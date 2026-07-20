from typing import Any, Callable, Coroutine, Protocol
from pydantic import BaseModel, ValidationError
import inspect
from pathlib import Path
import os
import functools

from remote.backends import (
    Subprocess,
    AnyBackendConfig,
    RemoteExecutionErrorResponse,
    RemoteExecutionError,
    RemoteExecutionProtocolError,
)
from remote.runtime import register_target
from remote.session import RemoteSession

EXECUTION_TEMPLATE = """
import asyncio
import json
import os
import sys
import traceback

def _write_result(result_file: str, data: str):
    with open(result_file, 'w') as f:
        f.write(data)

async def execute():
    result_file = os.environ.get('REMOTE_EXECUTION_RESULT_FILE')
    if not result_file:
        print("Error: REMOTE_EXECUTION_RESULT_FILE environment variable not set", file=sys.stderr)
        sys.exit(1)

    try:
        {import_model}
        {import_func}
        arg = {model_name}.model_validate_json({arg_json})
        res = await {func_name}(arg)
        _write_result(result_file, res.model_dump_json())
    except Exception as e:
        _write_result(result_file, json.dumps({{
            "remote_execution_error": True,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }}))
        print(f"Remote execution failed: {{e}}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(execute())

"""


class RemoteFunction[I: BaseModel, O: BaseModel](Protocol):
    """A decorated remote function: callable directly, or against a RemoteSession."""

    def __call__(
        self, arg: I, *, session: RemoteSession | None = None
    ) -> Coroutine[Any, Any, O]: ...


def remote[I: BaseModel, O: BaseModel](
    local_project_root: Path,
    backend: AnyBackendConfig = Subprocess(),
    timeout_millis: int = 300000,  # 5 minutes default
) -> Callable[[Callable[[I], Coroutine[Any, Any, O]]], RemoteFunction[I, O]]:
    """
    Decorator that executes a function remotely using the specified backend.

    The decorated function must:
    - Take exactly one parameter of type BaseModel (or subclass)
    - Return a BaseModel (or subclass)

    Applying the decorator has no side effects beyond registering the backend
    target. Backend images are built lazily on first call (unless disabled via
    REMOTE_BOX_AUTO_BUILD=false or auto_build_override) or explicitly via
    `remote-box build` / `remote.build_all()`.

    Args:
        local_project_root: Root directory of the project for resolving imports
        backend: Backend configuration (default: Subprocess)
        timeout_millis: Maximum execution time in milliseconds (default: 300000 = 5 minutes)

    Usage:
        class InputModel(BaseModel):
            name: str

        class OutputModel(BaseModel):
            greeting: str

        @remote(
            local_project_root=Path(__file__).parent,
            backend=Daytona(snapshot_name="my-project"),
            timeout_millis=60000
        )
        async def my_function(input: InputModel) -> OutputModel:
            return OutputModel(greeting=f"Hello {input.name}")

        # One-shot (fresh sandbox per call):
        result = await my_function(InputModel(name="World"))

        # Reusing one sandbox across calls:
        async with RemoteSession(backend=..., local_project_root=...) as session:
            await my_function(InputModel(name="a"), session=session)
            await my_function(InputModel(name="b"), session=session)
    """
    # Record the target so `remote-box build` / build_all() can find it.
    # Deliberately no network access or builds at import time.
    register_target(backend, local_project_root)

    def decorator(func: Callable[[I], Coroutine[Any, Any, O]]) -> RemoteFunction[I, O]:
        # Extract the actual output model class from the return type annotation.
        # For async functions, the annotation is the output type directly (not
        # wrapped in Coroutine). eval_str handles `from __future__ import annotations`.
        output_model_class: type[BaseModel] = inspect.get_annotations(func, eval_str=True)[
            "return"
        ]

        @functools.wraps(func)
        async def wrapper(arg: I, *, session: RemoteSession | None = None) -> O:
            # If we're already in remote execution mode, just call the function directly
            if os.environ.get("REMOTE_EXECUTION_MODE") == "1":
                return await func(arg)

            # Generate the Python program that re-imports the function and its input
            # model remotely and reconstructs the argument from JSON. The JSON is
            # embedded as a Python string literal via repr(), which round-trips any
            # string safely — unlike repr() of the model itself, this handles
            # datetimes, enums, nested models, etc. and is not an injection surface.
            python_code = EXECUTION_TEMPLATE.format(
                import_model=__get_import_path(type(arg), local_project_root),
                import_func=__get_import_path(func, local_project_root),
                model_name=type(arg).__name__,
                func_name=func.__name__,
                arg_json=repr(arg.model_dump_json()),
            )

            if session is not None:
                stdout_str = await session.run_code(python_code, timeout_millis=timeout_millis)
            else:
                # One-shot mode: acquire a fresh sandbox just for this call.
                async with RemoteSession(
                    backend=backend,
                    local_project_root=local_project_root,
                    timeout_millis=timeout_millis,
                ) as ephemeral:
                    stdout_str = await ephemeral.run_code(python_code)

            return __parse_response(output_model_class, stdout_str)  # type: ignore[return-value]

        return wrapper  # type: ignore[return-value]

    return decorator


def __parse_response(output_model_class: type[BaseModel], stdout_str: str) -> BaseModel:
    """Parse harness stdout as an error payload first, then as the output model."""
    # Error payloads carry an explicit sentinel field, so check them first — an
    # output model with lenient/defaulted fields can't accidentally swallow one.
    try:
        error_response = RemoteExecutionErrorResponse.model_validate_json(stdout_str)
    except ValidationError:
        pass
    else:
        raise RemoteExecutionError(error_response)

    try:
        return output_model_class.model_validate_json(stdout_str)
    except ValidationError as e:
        raise RemoteExecutionProtocolError(stdout_str) from e


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
