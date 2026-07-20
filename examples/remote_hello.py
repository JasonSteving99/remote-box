"""Cloud sandbox example with a standalone project root — NO pyproject.toml.

The image name is derived purely from the build context of this directory
(see the sibling Dockerfile).

One-time setup — API keys live in a .env.local at the REPO ROOT, which the
justfile loads automatically:

    cp .env.example .env.local   # at the repo root, then fill in your keys

Run end-to-end from this directory (creates a real sandbox):

    just hello          # Daytona (default)
    just hello e2b      # E2B

Or build/check images without running: `uv run remote-box build examples/ --check`.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel

from _backend import BACKEND
from remote import remote


class Question(BaseModel):
    a: int
    b: int


class Answer(BaseModel):
    total: int


@remote(local_project_root=Path(__file__).parent, backend=BACKEND)
async def add(arg: Question) -> Answer:
    return Answer(total=arg.a + arg.b)


async def main() -> None:
    result = await add(Question(a=20, b=22))
    print(f"remote sandbox says: {result.total}")


if __name__ == "__main__":
    asyncio.run(main())
