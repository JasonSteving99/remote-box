"""End-to-end tests for remote-box using the Subprocess backend.

These run the full pipeline — codegen, bash harness, subprocess execution, and
response parsing — without needing any cloud API keys.
"""

import asyncio
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel

from remote import (
    remote,
    RemoteExecutionError,
    RemoteSession,
    Subprocess,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class NestedModel(BaseModel):
    count: int


class RichInput(BaseModel):
    """Exercises types whose reprs don't round-trip as plain Python source."""

    name: str
    timestamp: datetime
    color: Color
    nested: NestedModel
    tricky_string: str


class RichOutput(BaseModel):
    summary: str
    year: int
    count_doubled: int


@remote(local_project_root=PROJECT_ROOT, backend=Subprocess())
async def process_rich(arg: RichInput) -> RichOutput:
    return RichOutput(
        summary=f"{arg.name}/{arg.color.value}/{arg.tricky_string}",
        year=arg.timestamp.year,
        count_doubled=arg.nested.count * 2,
    )


def test_rich_types_roundtrip():
    result = asyncio.run(
        process_rich(
            RichInput(
                name="hello",
                timestamp=datetime(2026, 7, 19, 12, 0, 0),
                color=Color.BLUE,
                nested=NestedModel(count=21),
                # Quotes, braces, newlines, and a heredoc-terminator-looking line
                tricky_string='she said "hi" {} \nEOF\n$(rm -rf /) \'quoted\'',
            )
        )
    )
    assert result.year == 2026
    assert result.count_doubled == 42
    assert result.summary.startswith("hello/blue/")
    assert 'she said "hi"' in result.summary


class FileOp(BaseModel):
    path: str
    content: str = ""


class FileResult(BaseModel):
    content: str


@remote(local_project_root=PROJECT_ROOT, backend=Subprocess())
async def write_file(arg: FileOp) -> FileResult:
    Path(arg.path).write_text(arg.content)
    return FileResult(content=arg.content)


@remote(local_project_root=PROJECT_ROOT, backend=Subprocess())
async def read_file(arg: FileOp) -> FileResult:
    return FileResult(content=Path(arg.path).read_text())


def test_session_write_then_read():
    """Calls inside `async with session:` pick the session up implicitly."""

    async def scenario() -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = str(Path(tmpdir) / "state.txt")
            async with RemoteSession(backend=Subprocess(), local_project_root=PROJECT_ROOT):
                await write_file(FileOp(path=target, content="persisted!"))
                result = await read_file(FileOp(path=target))
            return result.content

    assert asyncio.run(scenario()) == "persisted!"


def test_decorated_signature_exposes_only_the_input_model():
    """AI SDKs reflect tool signatures to build schemas — no session/kwargs allowed.

    Checked without following __wrapped__, i.e. on the wrapper actually invoked.
    """
    import inspect

    sig = inspect.signature(write_file, follow_wrapped=False)
    assert list(sig.parameters) == ["arg"]


def test_scope_exit_only_closes_owned_sessions():
    """`async with` on a pre-started session must not destroy the sandbox."""

    async def scenario() -> None:
        session = RemoteSession(backend=Subprocess(), local_project_root=PROJECT_ROOT)

        # Bare `async with` owns: sandbox created on entry, destroyed on exit.
        async with session:
            assert session._handle is not None
        assert session._handle is None

        # Explicitly started: scopes only activate; close() is the owner's call.
        await session.start()
        async with session:
            assert session._handle is not None
        assert session._handle is not None
        await session.close()
        assert session._handle is None

    asyncio.run(scenario())


def test_framework_lifecycle_with_pause_and_ref_resume():
    """The agent-framework flow: start, scoped calls, pause, rehydrate from ref, close."""
    from remote import SessionRef

    async def scenario() -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = str(Path(tmpdir) / "state.txt")

            session = await RemoteSession(
                backend=Subprocess(), local_project_root=PROJECT_ROOT
            ).start()
            async with session:
                await write_file(FileOp(path=target, content="survived the pause"))

            ref = await session.pause()
            assert isinstance(ref, SessionRef)
            assert ref.backend == "SUBPROCESS"
            assert ref.sandbox_id is None  # subprocess has no persistent sandbox

            # Re-entering a paused session transparently resumes it.
            async with session:
                assert (await read_file(FileOp(path=target))).content == "survived the pause"
            await session.close()

            # Rehydrating from a serialized ref (as a new process would).
            rehydrated = await RemoteSession.resume(
                SessionRef.model_validate_json(ref.model_dump_json()),
                backend=Subprocess(),
                local_project_root=PROJECT_ROOT,
            )
            async with rehydrated:
                result = await read_file(FileOp(path=target))
            await rehydrated.close()
            return result.content

    assert asyncio.run(scenario()) == "survived the pause"


def test_resume_rejects_mismatched_backend():
    from remote import Daytona, SessionRef

    async def scenario() -> None:
        ref = SessionRef(backend="DAYTONA", sandbox_id="abc123")
        with pytest.raises(ValueError, match="DAYTONA"):
            await RemoteSession.resume(
                ref, backend=Subprocess(), local_project_root=PROJECT_ROOT
            )
        # And the config type must exist in the error path both ways
        ref = SessionRef(backend="SUBPROCESS")
        with pytest.raises(ValueError, match="SUBPROCESS"):
            await RemoteSession.resume(
                ref,
                backend=Daytona(snapshot_name="x"),
                local_project_root=PROJECT_ROOT,
            )

    asyncio.run(scenario())


def test_concurrent_tasks_see_their_own_ambient_session():
    """Context vars snapshot per task: a task spawned outside a scope stays one-shot."""
    from remote.session import current_session

    async def scenario() -> tuple[bool, bool]:
        session = RemoteSession(backend=Subprocess(), local_project_root=PROJECT_ROOT)
        outside_task = asyncio.create_task(asyncio.sleep(0))  # snapshots empty context

        async with session:
            inside = current_session() is session
            spawned = asyncio.create_task(_ambient_is(session))
            inside_task = await spawned
        await outside_task
        outside = current_session() is None
        return inside and inside_task, outside

    async def _ambient_is(expected: RemoteSession) -> bool:
        return current_session() is expected

    inside_ok, outside_ok = asyncio.run(scenario())
    assert inside_ok
    assert outside_ok


class NoiseInput(BaseModel):
    value: int


class NoiseOutput(BaseModel):
    value: int


@remote(local_project_root=PROJECT_ROOT, backend=Subprocess())
async def noisy(arg: NoiseInput) -> NoiseOutput:
    # stdout noise must never corrupt the response channel
    print('{"value": 99999}')
    print("random logging output")
    return NoiseOutput(value=arg.value + 1)


def test_stdout_noise_is_isolated():
    assert asyncio.run(noisy(NoiseInput(value=1))).value == 2


class FailInput(BaseModel):
    message: str


class FailOutput(BaseModel):
    never: str


@remote(local_project_root=PROJECT_ROOT, backend=Subprocess())
async def always_fails(arg: FailInput) -> FailOutput:
    raise ValueError(arg.message)


def test_remote_error_carries_type_and_traceback():
    with pytest.raises(RemoteExecutionError) as exc_info:
        asyncio.run(always_fails(FailInput(message="boom")))

    err = exc_info.value
    assert err.error_type == "ValueError"
    assert err.error_message == "boom"
    assert "ValueError: boom" in err.remote_traceback
    assert "always_fails" in err.remote_traceback


class SleepInput(BaseModel):
    seconds: float


class SleepOutput(BaseModel):
    ok: bool


@remote(local_project_root=PROJECT_ROOT, backend=Subprocess(), timeout_millis=2000)
async def sleeper(arg: SleepInput) -> SleepOutput:
    await asyncio.sleep(arg.seconds)
    return SleepOutput(ok=True)


def test_timeout_enforced():
    with pytest.raises(TimeoutError):
        asyncio.run(sleeper(SleepInput(seconds=30)))


def test_decorator_registers_build_targets():
    from remote.runtime import _REGISTERED_TARGETS

    assert (Subprocess(), PROJECT_ROOT) in _REGISTERED_TARGETS


def test_auto_build_resolution_precedence(monkeypatch):
    from remote import Daytona
    from remote.runtime import AUTO_BUILD_ENV_VAR, resolve_auto_build

    config = Daytona(snapshot_name="x")

    # Default: no override, no env var -> True
    monkeypatch.delenv(AUTO_BUILD_ENV_VAR, raising=False)
    assert resolve_auto_build(config) is True

    # Env var alone controls unset configs
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "false")
    assert resolve_auto_build(config) is False
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "0")
    assert resolve_auto_build(config) is False
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "TRUE")
    assert resolve_auto_build(config) is True

    # Explicit per-config override beats the env var
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "false")
    assert resolve_auto_build(Daytona(snapshot_name="x", auto_build_override=True)) is True
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "true")
    assert resolve_auto_build(Daytona(snapshot_name="x", auto_build_override=False)) is False

    # Unrecognized env value fails loudly rather than silently building
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "flase")
    with pytest.raises(ValueError, match="REMOTE_BOX_AUTO_BUILD"):
        resolve_auto_build(config)

    # Configs without the field (Subprocess) always allow
    monkeypatch.setenv(AUTO_BUILD_ENV_VAR, "true")
    assert resolve_auto_build(Subprocess()) is True


TASK_MODULE_TEMPLATE = '''
from pathlib import Path
from pydantic import BaseModel
from remote import remote, Subprocess

class CrawlIn(BaseModel):
    x: int

class CrawlOut(BaseModel):
    x: int

@remote(local_project_root=Path({root!r}), backend=Subprocess())
async def crawled_func(arg: CrawlIn) -> CrawlOut:
    return CrawlOut(x=arg.x)
'''


@pytest.fixture
def isolated_registry(monkeypatch):
    """Keep CLI tests from building targets registered by other test modules."""
    from remote import runtime

    monkeypatch.setattr(runtime, "_REGISTERED_TARGETS", [])


def test_cli_build_crawls_directory(tmp_path, isolated_registry, capsys):
    from remote.cli import main

    (tmp_path / "tasks.py").write_text(TASK_MODULE_TEMPLATE.format(root=str(PROJECT_ROOT)))
    (tmp_path / "plain.py").write_text("VALUE = 1\n")
    # Files in skipped directories must not be imported (this one would explode)
    skipped = tmp_path / ".venv"
    skipped.mkdir()
    (skipped / "bad.py").write_text("raise RuntimeError('should never be imported')\n")

    assert main(["build", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 target(s): 1 ready" in out
    assert "(local)" in out  # subprocess backend has no image


def test_cli_build_fails_on_broken_file(tmp_path, isolated_registry, capsys):
    from remote.cli import main

    (tmp_path / "tasks.py").write_text(TASK_MODULE_TEMPLATE.format(root=str(PROJECT_ROOT)))
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")

    assert main(["build", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    # Good files still got built, but the broken one is reported and fails the run
    assert "1 target(s): 1 ready" in captured.out
    assert "broken.py" in captured.err
    assert "boom" in captured.err


def test_cli_env_file_flag(tmp_path, isolated_registry):
    import os
    from remote.cli import main

    (tmp_path / "tasks.py").write_text(TASK_MODULE_TEMPLATE.format(root=str(PROJECT_ROOT)))
    env_file = tmp_path / ".env.custom"
    env_file.write_text("REMOTE_BOX_TEST_SENTINEL=loaded\n")

    os.environ.pop("REMOTE_BOX_TEST_SENTINEL", None)
    try:
        assert main(["build", str(tmp_path / "tasks.py"), "--env-file", str(env_file)]) == 0
        assert os.environ.get("REMOTE_BOX_TEST_SENTINEL") == "loaded"
    finally:
        os.environ.pop("REMOTE_BOX_TEST_SENTINEL", None)


def test_cli_missing_env_file_errors(isolated_registry, capsys):
    from remote.cli import main

    assert main(["build", "whatever.py", "--env-file", "/nonexistent/.env"]) == 1
    assert "env file not found" in capsys.readouterr().err


def test_cli_check_reports_ready_without_building(tmp_path, isolated_registry, capsys):
    from remote.cli import main

    (tmp_path / "tasks.py").write_text(TASK_MODULE_TEMPLATE.format(root=str(PROJECT_ROOT)))

    assert main(["build", str(tmp_path), "--check"]) == 0
    out = capsys.readouterr().out
    assert "1 target(s): 1 ready" in out
    assert "BACKEND" in out and "STATUS" in out  # table header


def test_cli_check_reports_missing_image(tmp_path, isolated_registry, monkeypatch, capsys):
    from remote import Daytona
    from remote.backends import BackendType, MissingImageError
    from remote import runtime
    from remote.cli import main

    (tmp_path / "tasks.py").write_text(TASK_MODULE_TEMPLATE.format(root=str(PROJECT_ROOT)))

    class StubDaytonaBackend:
        built = False

        @staticmethod
        def ensure_built(config, local_project_root, *, allow_build):
            if allow_build:
                StubDaytonaBackend.built = True
                return
            raise MissingImageError("my-project-v9-deadbeef")

    monkeypatch.setitem(runtime.BACKEND_REGISTRY, BackendType.DAYTONA, StubDaytonaBackend)
    runtime.register_target(Daytona(snapshot_name="my-project"), PROJECT_ROOT)

    assert main(["build", str(tmp_path), "--check"]) == 1
    out = capsys.readouterr().out
    assert "would build" in out
    assert "my-project-v9-deadbeef" in out
    # --check must never actually build
    assert StubDaytonaBackend.built is False


def test_daytona_config_validates_resource_minimums_per_class():
    from pydantic import ValidationError

    from remote import Daytona

    # linux-vm requires at least 3 GB disk (Daytona's smallest VM shape).
    with pytest.raises(ValidationError, match="disk_gb >= 3"):
        Daytona(snapshot_name="x", sandbox_class="linux-vm", disk_gb=2)
    # The defaults satisfy every class.
    assert Daytona(snapshot_name="x", sandbox_class="linux-vm").disk_gb == 3
    assert Daytona(snapshot_name="x").sandbox_class == "container"
    # Only classes that make sense for a Linux Dockerfile are accepted.
    with pytest.raises(ValidationError):
        Daytona(snapshot_name="x", sandbox_class="windows")


def test_daytona_snapshot_name_includes_sandbox_class(tmp_path):
    from remote import Daytona
    from remote.backends.daytona import _snapshot_name

    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')

    container = _snapshot_name(Daytona(snapshot_name="proj"), tmp_path)
    vm = _snapshot_name(Daytona(snapshot_name="proj", sandbox_class="linux-vm"), tmp_path)
    assert container.endswith("-container")
    assert vm.endswith("-linux-vm")
    assert container.removesuffix("-container") == vm.removesuffix("-linux-vm")


def test_pause_semantics_declared_per_backend():
    from remote import Daytona, PauseSemantics, RemoteSession, Subprocess
    from remote.backends.daytona import DaytonaBackend
    from remote.backends.e2b import E2BBackend
    from remote.backends.subprocess import SubprocessBackend

    assert SubprocessBackend.pause_semantics(Subprocess()) is PauseSemantics.NOOP
    assert E2BBackend.pause_semantics(None) is PauseSemantics.SUSPEND
    assert (
        DaytonaBackend.pause_semantics(Daytona(snapshot_name="x")) is PauseSemantics.STOP
    )
    assert (
        DaytonaBackend.pause_semantics(Daytona(snapshot_name="x", sandbox_class="linux-vm"))
        is PauseSemantics.SUSPEND
    )
    # Exposed on the session without starting it.
    session = RemoteSession(backend=Subprocess(), local_project_root=PROJECT_ROOT)
    assert session.pause_semantics is PauseSemantics.NOOP


def test_daytona_pause_is_deterministic_not_fallback():
    """pause() picks pause vs stop from the config-declared class — it never
    calls the API speculatively and sniffs 'not supported' errors."""
    from unittest.mock import AsyncMock

    from remote.backends.daytona import DaytonaBackend, DaytonaHandle

    sandbox = AsyncMock()
    handle = DaytonaHandle(client=AsyncMock(), sandbox=sandbox, suspend_capable=True)
    asyncio.run(DaytonaBackend.pause(handle))
    sandbox.pause.assert_awaited_once()
    sandbox.stop.assert_not_awaited()

    sandbox = AsyncMock()
    handle = DaytonaHandle(client=AsyncMock(), sandbox=sandbox, suspend_capable=False)
    asyncio.run(DaytonaBackend.pause(handle))
    sandbox.stop.assert_awaited_once()
    sandbox.pause.assert_not_awaited()


def test_daytona_no_runners_error_is_wrapped_actionably(tmp_path, monkeypatch):
    """When Daytona has no runners for the requested class, ensure_built fails
    fast — before any build — with guidance on region_id / org enablement."""
    import daytona as daytona_sdk

    from remote import Daytona
    from remote.backends.daytona import DaytonaBackend

    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')

    class StubSnapshots:
        def get(self, name):
            raise daytona_sdk.DaytonaNotFoundError("not found")

        def create(self, params, on_logs=None):
            assert params.sandbox_class == daytona_sdk.SandboxClass.LINUX_VM
            raise daytona_sdk.DaytonaError(
                "Failed to create snapshot: No runners are configured in region 'us' "
                "for sandbox class 'linux-vm'. Try a different region or sandbox class."
            )

    class StubClient:
        snapshot = StubSnapshots()

    monkeypatch.setattr(daytona_sdk, "Daytona", lambda *a, **kw: StubClient())
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")

    config = Daytona(snapshot_name="proj", sandbox_class="linux-vm", region_id="us")
    with pytest.raises(ValueError, match="no 'linux-vm' runners in region 'us'"):
        DaytonaBackend.ensure_built(config, tmp_path, allow_build=True)
