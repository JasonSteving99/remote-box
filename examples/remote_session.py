"""Session example: one cloud sandbox shared across calls, with pause/resume.

Uses the same image as remote_hello.py (same directory = same build context =
same image).

One-time setup — API keys live in a .env.local at the REPO ROOT, which the
justfile loads automatically:

    cp .env.example .env.local   # at the repo root, then fill in your keys

Run from this directory:

    just session        # Daytona (default)
    just session e2b    # E2B
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel

from _backend import BACKEND
from remote import RemoteSession, remote


class WriteRequest(BaseModel):
    path: str
    content: str


class ReadRequest(BaseModel):
    path: str


class FileContent(BaseModel):
    content: str


@remote(local_project_root=Path(__file__).parent, backend=BACKEND)
async def write_file(arg: WriteRequest) -> FileContent:
    Path(arg.path).write_text(arg.content)
    return FileContent(content=arg.content)


@remote(local_project_root=Path(__file__).parent, backend=BACKEND)
async def read_file(arg: ReadRequest) -> FileContent:
    return FileContent(content=Path(arg.path).read_text())


async def main() -> None:
    session = RemoteSession(backend=BACKEND, local_project_root=Path(__file__).parent)
    print(f"pause semantics for this config: {session.pause_semantics.name}")

    await session.start()
    async with session:
        await write_file(WriteRequest(path="/tmp/state.txt", content="survived the pause"))
        print("wrote /tmp/state.txt in the sandbox")

    ref = await session.pause()
    print(f"paused; serializable ref: {ref.model_dump_json()}")

    # Re-entering transparently resumes the paused sandbox — same filesystem.
    async with session:
        result = await read_file(ReadRequest(path="/tmp/state.txt"))
        print(f"read back after resume: {result.content!r}")

    await session.close()
    print("sandbox destroyed")


if __name__ == "__main__":
    asyncio.run(main())
