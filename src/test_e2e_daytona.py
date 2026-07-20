"""End-to-end tests for remote-box against real Daytona sandboxes.

These create real cloud sandboxes (marked `e2e`; they cost time and money):
- state persists across consecutive calls sharing a RemoteSession
- state does NOT persist across session-less calls (fresh sandbox per call)
- two concurrent sessions do NOT share a filesystem

Requires DAYTONA_API_KEY (loaded from .env.local if present); skipped otherwise.
The snapshot is built automatically from the project Dockerfile on first run.
The remote functions live in src/e2e_tasks.py — the sandbox imports that module,
so it must stay free of test-only dependencies like pytest.

Run with: uv run pytest src/test_e2e_daytona.py -q  (or `just e2e`)
"""

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from remote import RemoteExecutionError, RemoteSession
from src.e2e_tasks import (
    BACKEND,
    PROJECT_ROOT,
    ReadRequest,
    WriteRequest,
    read_file,
    write_file,
)

load_dotenv(Path(__file__).parent.parent / ".env.local")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("DAYTONA_API_KEY"),
        reason="DAYTONA_API_KEY not set — skipping Daytona e2e tests",
    ),
]


def test_session_persists_state_across_calls():
    """Both calls hit the SAME sandbox, so call #2 sees the file from call #1."""

    async def scenario() -> str:
        async with RemoteSession(backend=BACKEND, local_project_root=PROJECT_ROOT) as session:
            await write_file(
                WriteRequest(path="/tmp/session_state.txt", content="written in call #1"),
                session=session,
            )
            result = await read_file(
                ReadRequest(path="/tmp/session_state.txt"), session=session
            )
            return result.content

    assert asyncio.run(scenario()) == "written in call #1"


def test_sessionless_calls_get_fresh_sandboxes():
    """Each session-less call gets a fresh sandbox, so the written file is gone."""

    async def scenario() -> None:
        await write_file(WriteRequest(path="/tmp/oneshot_state.txt", content="ephemeral"))
        await read_file(ReadRequest(path="/tmp/oneshot_state.txt"))

    with pytest.raises(RemoteExecutionError) as exc_info:
        asyncio.run(scenario())
    assert exc_info.value.error_type == "FileNotFoundError"


def test_separate_sessions_do_not_share_filesystem():
    """A file written in session A is visible in A but invisible to concurrent session B."""

    async def scenario() -> str:
        async with (
            RemoteSession(backend=BACKEND, local_project_root=PROJECT_ROOT) as session_a,
            RemoteSession(backend=BACKEND, local_project_root=PROJECT_ROOT) as session_b,
        ):
            await write_file(
                WriteRequest(path="/tmp/isolation_state.txt", content="session A's secret"),
                session=session_a,
            )
            result = await read_file(
                ReadRequest(path="/tmp/isolation_state.txt"), session=session_a
            )
            assert result.content == "session A's secret"

            with pytest.raises(RemoteExecutionError) as exc_info:
                await read_file(
                    ReadRequest(path="/tmp/isolation_state.txt"), session=session_b
                )
            return exc_info.value.error_type

    assert asyncio.run(scenario()) == "FileNotFoundError"
