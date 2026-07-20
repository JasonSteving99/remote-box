"""Reusable sandbox sessions for consecutive remote calls against one environment."""

import asyncio
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Self

from remote.backends import AnyBackendConfig, PauseSemantics, SessionRef
from remote.runtime import (
    BACKEND_REGISTRY,
    ensure_built_once,
    format_harness,
    resolve_auto_build,
)

# The ambient session for the current async context. Set by `async with session:`
# and read by @remote-decorated functions. This is the ONLY way calls join a
# session — decorated functions deliberately take just their input model, so
# frameworks that reflect tool signatures (e.g. AI agent SDKs) never see a
# session parameter they'd try to fill in.
_current_session: ContextVar["RemoteSession | None"] = ContextVar(
    "remote_box_current_session", default=None
)


def current_session() -> "RemoteSession | None":
    """The session governing the current async context, if any."""
    return _current_session.get()


class RemoteSession:
    """
    A live sandbox that persists across multiple remote function calls.

    Entering the session makes it ambient for the enclosed code: any
    @remote-decorated call inside the block runs in this session's sandbox, so
    state written to its filesystem in one call is visible to the next:

        async with RemoteSession(
            backend=Daytona(snapshot_name="my-project"),
            local_project_root=Path(__file__).parent,
        ) as session:
            await write_file(WriteInput(path="/tmp/x", data="hi"))
            result = await read_file(ReadInput(path="/tmp/x"))
        # sandbox destroyed on exit

    The session's backend determines the execution environment; a decorated
    function called inside the block runs there regardless of the backend in
    its own decorator config.

    Ownership rule: `async with` on a session that was NOT explicitly started
    owns the sandbox — it is created on entry and destroyed on exit (the
    behavior shown above). If the session was already started via
    `await session.start()`, `async with` only activates it for the scope and
    the external owner keeps control — ideal for frameworks that manage the
    lifecycle themselves:

        session = RemoteSession(backend=..., local_project_root=...)
        await session.start()
        async with session:               # per tool-call scope
            await some_tool(input)        # session picked up implicitly
        ref = await session.pause()       # e.g. while an agent awaits human input
        async with session:               # transparently resumes
            await another_tool(input)
        await session.close()             # only at final teardown

    `pause()` returns a serializable `SessionRef`; `RemoteSession.resume(ref,
    backend=..., local_project_root=...)` reattaches to the same sandbox even
    from a different process.

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
        self._paused = False
        self._lock = asyncio.Lock()
        # (contextvar token, owns) per `async with` entry; a stack because
        # scopes can nest and tokens must be reset in reverse order.
        self._scopes: list[tuple[Token, bool]] = []

    @property
    def python_cmd(self) -> str:
        return self._backend_impl.PYTHON_CMD

    @property
    def pause_semantics(self) -> PauseSemantics:
        """
        What `pause()` will actually preserve, per the backend config.

        SUSPEND: memory and running processes are frozen and survive resume.
        STOP: the filesystem persists but processes are killed (e.g. Daytona's
        default 'container' sandbox class — use sandbox_class='linux-vm' for
        SUSPEND). NOOP: no persistent sandbox. Config-derived, so it can be
        checked before starting the session.
        """
        return self._backend_impl.pause_semantics(self._backend)

    @property
    def ref(self) -> SessionRef:
        """
        Serializable pointer to this session's sandbox.

        Persist it (it carries no secrets) and reattach later — even from a
        different process — with `RemoteSession.resume()`.
        """
        if self._handle is None:
            raise RuntimeError("RemoteSession is not started; no sandbox to reference.")
        return SessionRef(
            backend=self._backend.type.name,
            sandbox_id=self._backend_impl.sandbox_id(self._handle),
        )

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

    async def pause(self) -> SessionRef:
        """
        Pause the sandbox so it stops consuming compute while idle, and return
        a SessionRef for resuming later.

        Re-entering the session (`async with session:`) resumes it in place;
        `RemoteSession.resume(ref, ...)` reattaches from another process. Prefer
        calling this explicitly at genuine idle points (e.g. an agent waiting on
        human input) — pausing between back-to-back calls just adds latency.

        Whether running processes survive the pause depends on the backend
        config — check `session.pause_semantics`: only SUSPEND preserves them;
        STOP keeps the filesystem but kills processes.
        """
        if self._handle is None:
            raise RuntimeError("RemoteSession is not started; nothing to pause.")
        async with self._lock:
            if not self._paused:
                await self._backend_impl.pause(self._handle)
                self._paused = True
        return self.ref

    async def _resume_if_paused(self) -> None:
        if not self._paused:
            return
        async with self._lock:
            if self._paused:
                await self._backend_impl.resume(self._handle)
                self._paused = False

    @classmethod
    async def resume(
        cls,
        ref: SessionRef,
        *,
        backend: AnyBackendConfig,
        local_project_root: Path,
        timeout_millis: int = 300000,
    ) -> Self:
        """
        Reattach to an existing sandbox from a SessionRef, resuming it if paused.

        Works across process boundaries: a framework can persist the ref while
        an agent idles, then rehydrate the session when work continues. The ref
        intentionally carries no credentials, so the same backend config used to
        create the session must be supplied again here.
        """
        if ref.backend != backend.type.name:
            raise ValueError(
                f"SessionRef points at a {ref.backend} sandbox but resume() was "
                f"given a {backend.type.name} backend config."
            )
        session = cls(
            backend=backend,
            local_project_root=local_project_root,
            timeout_millis=timeout_millis,
        )
        session._handle = await session._backend_impl.reconnect(
            backend, ref.sandbox_id, local_project_root, timeout_millis
        )
        return session

    async def close(self) -> None:
        """Destroy the sandbox. Safe to call multiple times."""
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        self._paused = False
        await self._backend_impl.release(handle)

    async def __aenter__(self) -> Self:
        # Ownership rule: entering an un-started session starts AND owns it, so
        # the sandbox dies on exit. Entering an already-started session only
        # activates it for this scope (resuming if paused) — the external owner
        # keeps control of pause/close.
        owns = self._handle is None
        if owns:
            await self.start()
        else:
            await self._resume_if_paused()
        token = _current_session.set(self)
        self._scopes.append((token, owns))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        token, owns = self._scopes.pop()
        _current_session.reset(token)
        if owns:
            await self.close()

    async def run_code(self, python_code: str, timeout_millis: int | None = None) -> str:
        """
        Run generated Python code in the session's sandbox and return raw harness stdout.

        This is the low-level entry point used by the @remote decorator; most users
        should call decorated functions inside `async with session:` instead.
        """
        if self._handle is None:
            raise RuntimeError(
                "RemoteSession is not started. Use 'async with RemoteSession(...)' or "
                "'await session.start()' before making calls."
            )
        await self._resume_if_paused()
        bash_script = format_harness(self.python_cmd, python_code)
        async with self._lock:
            return await self._backend_impl.run(
                self._handle, bash_script, timeout_millis or self._timeout_millis
            )
