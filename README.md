# Remote Box

Type-safe remote Python function execution framework with multiple backend support.

## Installation

```bash
uv add remote-box
```

## Quick Start

Execute Python functions on remote machines with type safety:

```python
from pathlib import Path
from pydantic import BaseModel
from remote import remote, E2B, BackendShell

class Input(BaseModel):
    name: str

class Output(BaseModel):
    greeting: str

@remote(
    local_project_root=Path(__file__).parent,
    backend=E2B(
        shell=BackendShell.BASH4,
        template_prefix="my-project"
    )
)
async def greet(input: Input) -> Output:
    # This code runs on a remote E2B sandbox!
    return Output(greeting=f"Hello {input.name}!")

# Usage
result = await greet(Input(name="World"))
print(result.greeting)  # "Hello World!"
```

## Features

- **Type-safe**: Inputs/outputs validated using Pydantic models
- **Multiple backends**:
  - [E2B](https://e2b.dev) - Execute on remote secure sandboxes
  - Subprocess - Local execution for development
- **Async-first**: Built on asyncio for high performance
- **Automatic serialization**: No manual JSON handling needed

## Backends

### E2B (Production)
Execute code on remote secure sandboxes via [E2B](https://e2b.dev). Perfect for:
- Running untrusted code safely
- Scaling compute workloads
- Isolating execution environments

### Subprocess (Development)
Execute code in local subprocesses. Ideal for development and testing.

## License

MIT
