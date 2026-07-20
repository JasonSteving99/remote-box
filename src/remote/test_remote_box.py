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
    """Consecutive calls through one session share an environment."""

    async def scenario() -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = str(Path(tmpdir) / "state.txt")
            async with RemoteSession(
                backend=Subprocess(), local_project_root=PROJECT_ROOT
            ) as session:
                await write_file(FileOp(path=target, content="persisted!"), session=session)
                result = await read_file(FileOp(path=target), session=session)
            return result.content

    assert asyncio.run(scenario()) == "persisted!"


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
