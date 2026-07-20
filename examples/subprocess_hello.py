"""Minimal remote-box example — note: this directory has NO pyproject.toml.

Run from this directory (no API keys needed):

    just subprocess
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel

from remote import Subprocess, remote


class Input(BaseModel):
    name: str


class Output(BaseModel):
    greeting: str


@remote(local_project_root=Path(__file__).parent, backend=Subprocess())
async def greet(arg: Input) -> Output:
    return Output(greeting=f"Hello {arg.name}!")


async def main() -> None:
    result = await greet(Input(name="World"))
    print(result.greeting)


if __name__ == "__main__":
    asyncio.run(main())
