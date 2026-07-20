"""Remote task definitions for the Daytona e2e tests.

Kept separate from the pytest suite because the sandbox imports THIS module to
resolve the decorated functions — so it must import cleanly with only the
project's production dependencies (no pytest), and it must live under src/ so
the Dockerfile bakes it into the snapshot.
"""

from pathlib import Path

from pydantic import BaseModel

from remote import remote, Daytona

PROJECT_ROOT = Path(__file__).parent.parent
BACKEND = Daytona(snapshot_name="my-project")


class WriteRequest(BaseModel):
    path: str
    content: str


class ReadRequest(BaseModel):
    path: str


class FileContent(BaseModel):
    content: str


@remote(local_project_root=PROJECT_ROOT, backend=BACKEND)
async def write_file(arg: WriteRequest) -> FileContent:
    Path(arg.path).write_text(arg.content)
    return FileContent(content=arg.content)


@remote(local_project_root=PROJECT_ROOT, backend=BACKEND)
async def read_file(arg: ReadRequest) -> FileContent:
    return FileContent(content=Path(arg.path).read_text())
