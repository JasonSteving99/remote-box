"""Reusable sandbox sessions for consecutive remote calls against one environment."""

import asyncio
from pathlib import Path
from typing import Any, Self

from remote.backends import AnyBackendConfig
from remote.runtime import (
    BACKEND_REGISTRY,
    ensure_built_once,
    format_harness,
    resolve_auto_build,
)


class RemoteSession:
    """
    A live sandbox that persists across multiple remote function calls.

    State written to the sandbox filesystem in one call is visible to subsequent
    calls in the same session — e.g. write a file in one call, read it in the next:

        async with RemoteSession(
            backend=Daytona(snapshot_name="my-project"),
            local_project_root=Path(__file__).parent,
        ) as session:
            await write_file(WriteInput(path="/tmp/x", data="hi"), session=session)
            result = await read_file(ReadInput(path="/tmp/x"), session=session)
        # sandbox destroyed on exit

    The session's backend determines the execution environment; a decorated
    function called with `session=` runs there regardless of the backend in its
    own decorator config.

    Calls within one session are serialized with an internal lock, so it is safe
    to share a session between concurrent tasks (they just won't run in parallel).

    In notebooks (which support top-level await) `async with` works directly, or
    use the explicit `await session.start()` / `await session.close()` pair.
    """

    def __init__(
        self,
        backend: AnyBackendConfig,
        local_project_root: Path,
        timeout_millis: int = 300000,  # 5 minutes default per call
    ):
        """
        Args:
            backend: Backend configuration for the sandbox
            local_project_root: Root directory of the project for resolving imports
            timeout_millis: Default per-call timeout (individual calls may override)
        """
        self._backend = backend
        self._backend_impl = BACKEND_REGISTRY[backend.type]
        self._local_project_root = local_project_root
        self._timeout_millis = timeout_millis
        self._handle: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def python_cmd(self) -> str:
        return self._backend_impl.PYTHON_CMD

    async def start(self) -> Self:
        """Ensure the backend image exists (building if auto-build allows) and acquire the sandbox."""
        if self._handle is not None:
            return self

        # Image builds are blocking and can take minutes — keep the event loop free.
        await asyncio.to_thread(
            ensure_built_once,
            self._backend,
            self._local_project_root,
            allow_build=resolve_auto_build(self._backend),
        )
        self._handle = await self._backend_impl.acquire(
            self._backend, self._local_project_root, self._timeout_millis
        )
        return self

    async def close(self) -> None:
        """Destroy the sandbox. Safe to call multiple times."""
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        await self._backend_impl.release(handle)

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def run_code(self, python_code: str, timeout_millis: int | None = None) -> str:
        """
        Run generated Python code in the session's sandbox and return raw harness stdout.

        This is the low-level entry point used by the @remote decorator; most users
        should call decorated functions with `session=` instead.
        """
        if self._handle is None:
            raise RuntimeError(
                "RemoteSession is not started. Use 'async with RemoteSession(...)' or "
                "'await session.start()' before making calls."
            )
        bash_script = format_harness(self.python_cmd, python_code)
        async with self._lock:
            return await self._backend_impl.run(
                self._handle, bash_script, timeout_millis or self._timeout_millis
            )
