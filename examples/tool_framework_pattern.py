"""How an AI agent framework can own a `@tool(sandboxed=True)` decorator that
uses remote-box internally, without ever asking its users to import `remote`.

Run from this directory (no API keys needed — uses the Subprocess backend):

    just tool-pattern

This demonstrates BOTH a correct and a buggy version of such a decorator, side
by side: the approval gate below blocks on `_HUMAN_RESPONDED`, an asyncio.Event
that only THIS process ever sets. A freshly spawned sandbox process gets its
own brand-new, never-set copy of that Event on import. So:

- The correct decorator (`tool`) checks `in_remote_execution()` and skips the
  gate entirely inside the sandbox -> finishes immediately.
- The naive decorator (`naive_tool`) has no such check, so its gate re-fires
  inside the sandbox, where nothing will ever set that fresh Event -> it hangs
  until the bounded timeout and the failure surfaces as a real timeout, not a
  silently-wrong success.
"""

import asyncio
import functools
from pathlib import Path

from pydantic import BaseModel

from remote import RemoteExecutionError, Subprocess, in_remote_execution, remote

# Stand-in for a real approval channel (Slack, a CLI prompt, a web UI) — only
# ever reachable in the process that has an actual human attached to it.
_HUMAN_RESPONDED = asyncio.Event()


async def _await_human_approval() -> None:
    # Bounded so a broken integration fails fast instead of hanging forever.
    await asyncio.wait_for(_HUMAN_RESPONDED.wait(), timeout=2)


def tool(sandboxed: bool = False):
    """Correct: checks in_remote_execution() before gating."""

    def decorator(raw_func):
        dispatch = (
            remote(local_project_root=Path(__file__).parent, backend=Subprocess())(raw_func)
            if sandboxed
            else raw_func
        )

        @functools.wraps(raw_func)
        async def wrapper(arg):
            if in_remote_execution():
                # Already inside the sandbox: the gate below already ran once
                # on the host before remote-box dispatched here.
                return await raw_func(arg)
            if sandboxed:
                await _await_human_approval()
            return await dispatch(arg)

        return wrapper

    return decorator


def naive_tool(sandboxed: bool = False):
    """Buggy: no in_remote_execution() check — the bug in_remote_execution() exists to prevent."""

    def decorator(raw_func):
        dispatch = (
            remote(local_project_root=Path(__file__).parent, backend=Subprocess())(raw_func)
            if sandboxed
            else raw_func
        )

        @functools.wraps(raw_func)
        async def wrapper(arg):
            if sandboxed:
                await _await_human_approval()
            return await dispatch(arg)

        return wrapper

    return decorator


class DeleteInput(BaseModel):
    path: str


class DeleteResult(BaseModel):
    deleted: str


@tool(sandboxed=True)
async def delete_file(arg: DeleteInput) -> DeleteResult:
    return DeleteResult(deleted=arg.path)


@naive_tool(sandboxed=True)
async def naive_delete_file(arg: DeleteInput) -> DeleteResult:
    return DeleteResult(deleted=arg.path)


async def main() -> None:
    # simulate the human having already approved - this event is in-memory in the
    # local process and only approves the host-side call, not the real impl that runs
    # in the remote box.
    _HUMAN_RESPONDED.set()

    result = await delete_file(DeleteInput(path="/tmp/example.txt"))
    print(f"correct decorator: succeeded -> {result.deleted}")

    try:
        await naive_delete_file(DeleteInput(path="/tmp/example.txt"))
        print("naive decorator: succeeded (unexpected!)")
    except RemoteExecutionError as e:
        print(f"naive decorator: gate re-fired inside the sandbox and hung -> {e.error_type}")


if __name__ == "__main__":
    asyncio.run(main())
