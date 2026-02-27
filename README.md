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
from remote import remote, E2B

class Input(BaseModel):
    name: str

class Output(BaseModel):
    greeting: str

@remote(
    local_project_root=Path(__file__).parent,
    backend=E2B(
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
  - [E2B](https://e2b.dev) — Remote secure sandboxes
  - [Daytona](https://daytona.io) — Remote sandboxes with snapshot-based images
  - Subprocess — Local execution for development/testing
- **Async-first**: Built on asyncio for high performance
- **Automatic serialization**: No manual JSON handling needed
- **Pre-checks at import time**: Backend validation and snapshot/template creation happens once when the decorator is applied, not on every call

## Backends

### E2B (Production)

Execute code on remote secure sandboxes via [E2B](https://e2b.dev).

```python
from remote import remote, E2B

@remote(
    local_project_root=Path(__file__).parent,
    backend=E2B(
        template_prefix="my-project",   # template name becomes "my-project-v{version}"
        e2b_api_key="...",              # or set E2B_API_KEY env var
        cpu_count=2,
        memory_mb=2048,
    )
)
async def my_func(input: Input) -> Output: ...
```

The template is built automatically from your `Dockerfile` the first time it's needed (keyed by `{template_prefix}-v{version}` where version comes from `pyproject.toml`).

### Daytona (Production)

Execute code on remote sandboxes via [Daytona](https://daytona.io). Snapshots are created automatically from your `Dockerfile`.

```python
from remote import remote, Daytona

@remote(
    local_project_root=Path(__file__).parent,
    backend=Daytona(
        snapshot_name="my-project",     # snapshot name becomes "my-project-v{version}"
        daytona_api_key="...",          # or set DAYTONA_API_KEY env var
        cpu_count=2,
        memory_gb=2,
        disk_gb=5,
    )
)
async def my_func(input: Input) -> Output: ...
```

### Subprocess (Development)

Execute code in a local subprocess via `uv run`. Ideal for development and testing — no API keys or Docker required.

```python
from remote import remote, Subprocess, BackendShell

@remote(
    local_project_root=Path(__file__).parent,
    backend=Subprocess(shell=BackendShell.ZSH)  # ZSH default on macOS; use BASH4 on Linux
)
async def my_func(input: Input) -> Output: ...
```

## Configuration Reference

### `remote` decorator

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `local_project_root` | `Path` | required | Root directory used to resolve imports and locate `Dockerfile`/`pyproject.toml` |
| `backend` | `AnyBackendConfig` | `Subprocess(shell=BackendShell.ZSH)` | Backend to execute on |
| `timeout_millis` | `int` | `300000` | Max execution time in ms (default 5 minutes) |

### `E2B` config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `template_prefix` | required | Prefix for E2B template name (`{prefix}-v{version}`) |
| `e2b_api_key` | `None` | API key (falls back to `E2B_API_KEY` env var) |
| `template_version` | `None` | Override version; defaults to `pyproject.toml` version |
| `dockerfile_path` | `None` | Path to Dockerfile; defaults to `Dockerfile` in project root |
| `cpu_count` | `1` | CPUs to allocate |
| `memory_mb` | `1024` | Memory in MB to allocate |

### `Daytona` config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `snapshot_name` | required | Prefix for Daytona snapshot name (`{name}-v{version}`) |
| `daytona_api_key` | `None` | API key (falls back to `DAYTONA_API_KEY` env var) |
| `snapshot_version` | `None` | Override version; defaults to `pyproject.toml` version |
| `dockerfile_path` | `None` | Path to Dockerfile; defaults to `Dockerfile` in project root |
| `cpu_count` | `1` | CPUs to allocate |
| `memory_gb` | `1` | Memory in GB to allocate |
| `disk_gb` | `3` | Disk in GB to allocate |

### `Subprocess` config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `shell` | required | `BackendShell.ZSH` or `BackendShell.BASH4` |

## License

MIT
