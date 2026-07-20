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

The first call builds the sandbox image from your `Dockerfile` automatically (see
[Building images](#building-images-local-dev-vs-cicd) to move that into CI/CD instead of dynamically at runtime).

## Features

- **Type-safe**: Inputs/outputs validated using Pydantic models; arguments travel as JSON, so `datetime`, enums, and nested models all work
- **Reusable sandboxes**: `RemoteSession` runs consecutive calls in the *same* sandbox — write a file in one call, read it in the next 
- **Agent-framework ready**: explicit `start`/`pause`/`resume`/`close` lifecycle with serializable session refs, so sandboxes can survive agent idle periods and process restarts
- **Multiple backends**:
  - [E2B](https://e2b.dev) — Remote secure sandboxes
  - [Daytona](https://daytona.io) — Remote sandboxes with snapshot-based images
  - Subprocess — Local execution for development/testing
- **Async-first**: Built on asyncio for high performance
- **No import-time side effects**: images build lazily on first call, or explicitly via the `remote-box build` CLI (recommended for production)
- **Real remote errors**: exceptions raised remotely surface locally as `RemoteExecutionError` with the remote traceback attached

## Sessions: consecutive calls in one sandbox

By default every call gets a fresh sandbox that is destroyed afterwards. To share
one sandbox (and its filesystem) across calls, run them inside a `RemoteSession`
scope — calls pick the session up **implicitly**:

```python
from remote import RemoteSession, Daytona

async with RemoteSession(
    backend=Daytona(snapshot_name="my-project"),
    local_project_root=Path(__file__).parent,
):
    await write_file(WriteInput(path="/tmp/state.json", data="..."))
    result = await read_file(ReadInput(path="/tmp/state.json"))
# sandbox destroyed on exit
```

There is deliberately **no `session=` parameter** on decorated functions: their
signature is exactly the input model, so frameworks that reflect tool signatures
(e.g. AI agent SDKs building tool schemas) never see session plumbing. Session
propagation uses a context variable, which flows correctly across `await`
boundaries and into `asyncio.create_task`.

Notes:

- The **session's** backend config decides where the code runs; the decorator's own
  backend is only used for session-less calls.
- Calls within a session are serialized with an internal lock, so sharing a session
  between concurrent tasks is safe (they just won't run in parallel).
- In notebooks, `async with` works at the top level, or use
  `await session.start()` / `await session.close()` explicitly.
- E2B sandboxes have a lifetime TTL (`sandbox_ttl_seconds`, default 600s) that is
  refreshed before every call, so a session stays alive as long as you keep using it.

## Explicitly managing sandbox lifecycles: start, pause, resume, close

`async with` on a session that was **not** explicitly started owns the sandbox: it
is created on entry and destroyed on exit (the example above). If the session was
already started with `await session.start()`, `async with` only *activates* it for
the scope — whoever started it decides when it dies. This lets a framework (e.g.
an AI agent runtime that auto-sandboxes tools) manage one sandbox across many tool
calls:

```python
session = RemoteSession(backend=Daytona(...), local_project_root=...)
await session.start()                 # framework owns the sandbox

async with session:                   # per tool call — activates, doesn't destroy
    result = await some_tool(input)   # session picked up implicitly

ref = await session.pause()           # agent idles (e.g. waiting on human input):
                                      # sandbox stops consuming compute

async with session:                   # transparently resumes the paused sandbox
    result = await another_tool(input)

await session.close()                 # only at agent termination
```

`pause()` returns a `SessionRef` — a small, secret-free, JSON-serializable pointer
to the sandbox (`session.ref` works any time after start). Persist it anywhere and
reattach later, **even from a different process**:

```python
# process A
ref_json = (await session.pause()).model_dump_json()

# process B, an hour later
session = await RemoteSession.resume(
    SessionRef.model_validate_json(ref_json),
    backend=Daytona(...),             # refs carry no credentials — config is re-supplied
    local_project_root=...,
)
async with session:
    result = await next_tool(input)
```

Pause/resume semantics per backend:

| Backend | `pause()` | Preserved | Cross-process resume |
|---------|-----------|-----------|----------------------|
| Daytona | native pause; falls back to stop if the sandbox class doesn't support pausing | filesystem and memory (filesystem only on the stop fallback) | `client.get(id)` + start |
| E2B | native sandbox pause | filesystem **and** memory | `AsyncSandbox.connect(id)` |
| Subprocess | no-op (nothing to pause) | local filesystem trivially | fresh handle |

Prefer pausing at genuine idle points rather than after every call — a
pause/resume round trip costs a few seconds on the cloud backends. Daytona users
can also set an auto-pause interval on the platform side as a safety net.

## Building images: local dev vs CI/CD

E2B templates and Daytona snapshots are built from your `Dockerfile` and cached
under the name `{prefix}-v{version}-{dockerfile_hash}` (version from
`pyproject.toml` unless overridden). Editing the Dockerfile automatically produces
a new image name; **editing source files that the Dockerfile COPYs does not** —
bump your project version after source changes so a fresh image is built.

Whether a missing image may be built lazily at runtime is controlled by the
**`REMOTE_BOX_AUTO_BUILD` environment variable** (default: true), so switching
between local dev and production requires **no source changes** no matter how many
`@remote` functions you have:

1. **Local dev / notebooks (default)** — leave `REMOTE_BOX_AUTO_BUILD` unset: the
   first call that needs a missing image builds it on the spot.
2. **Production (recommended)** — set `REMOTE_BOX_AUTO_BUILD=false` in the
   deployment environment and build ahead of time in CI/CD with the CLI:

   ```bash
   remote-box build src/                     # directory: crawls *.py recursively
   remote-box build src/my_tasks.py          # single file
   remote-box build myproject.tasks          # dotted module name
   remote-box build src/ --env-file .env.local   # custom env file for API keys
   remote-box build src/ --check             # discovery only: report, build nothing
   ```

   The CLI imports the target(s), finds every `@remote`-decorated function, and
   builds any missing images (the env var does not apply to the CLI — explicit
   builds always build). At runtime, a missing image then raises
   `MissingImageError` instead of paying the build cost (or requiring build
   permissions) in production.

   Directory crawls skip hidden, venv, and cache directories, and a file that
   fails to import fails the run (exit 1) after building everything that did
   import — a broken file might contain `@remote` functions, so CI must not
   pass silently. API keys load from `.env` automatically; use `--env-file`
   for an alternate file like `.env.local`.

   `--check` runs discovery only and reports each target as `[ready]`,
   `[would build]` (image missing), or `[error]` (bad config/credentials)
   without building anything. It exits 0 only when everything is ready, so a
   deploy pipeline can gate on it: build images in CI, then `--check` as a
   pre-deploy verification that nothing slipped through. Also available
   programmatically as `remote.runtime.check_all()`.

   The same thing is available programmatically via `remote.build_all()` after
   importing your task modules.

For the rare case where one specific config must pin its behavior regardless of
the environment, set `auto_build_override=True/False` on that config — it takes
precedence over the env var, but it means editing source to change environments,
so prefer the env var.

## Backends

### E2B (Production)

Execute code on remote secure sandboxes via [E2B](https://e2b.dev).

```python
from remote import remote, E2B

@remote(
    local_project_root=Path(__file__).parent,
    backend=E2B(
        template_prefix="my-project",   # template name becomes "my-project-v{version}-{hash}"
        e2b_api_key="...",              # or set E2B_API_KEY env var
        cpu_count=2,
        memory_mb=2048,
    )
)
async def my_func(input: Input) -> Output: ...
```

### Daytona (Production)

Execute code on remote sandboxes via [Daytona](https://daytona.io).

```python
from remote import remote, Daytona

@remote(
    local_project_root=Path(__file__).parent,
    backend=Daytona(
        snapshot_name="my-project",     # snapshot name becomes "my-project-v{version}-{hash}"
        daytona_api_key="...",          # or set DAYTONA_API_KEY env var
        cpu_count=2,
        memory_gb=2,
        disk_gb=5,
    )
)
async def my_func(input: Input) -> Output: ...
```

### Subprocess (Development)

Execute code in a local subprocess via `uv run` (requires `bash` and `uv`). Ideal
for development and testing — no API keys or Docker required.

```python
from remote import remote, Subprocess

@remote(
    local_project_root=Path(__file__).parent,
    backend=Subprocess()
)
async def my_func(input: Input) -> Output: ...
```

## Error handling

```python
from remote import RemoteExecutionError, RemoteExecutionProtocolError, MissingImageError

try:
    result = await my_func(Input(...))
except RemoteExecutionError as e:
    print(e.error_type)        # e.g. "ValueError" — exception type raised remotely
    print(e.error_message)
    print(e.remote_traceback)  # full remote traceback
```

- `RemoteExecutionError` — your function raised an exception remotely
- `RemoteExecutionProtocolError` — the sandbox response couldn't be parsed at all
  (carries `raw_output` for debugging); interpreter-level failures (import errors,
  crashes) surface the remote stderr in the raised error
- `MissingImageError` — auto-building is disabled (`REMOTE_BOX_AUTO_BUILD=false`)
  and the image hasn't been built yet

## Configuration Reference

### `remote` decorator

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `local_project_root` | `Path` | required | Root directory used to resolve imports and locate `Dockerfile`/`pyproject.toml` |
| `backend` | `AnyBackendConfig` | `Subprocess()` | Backend to execute on |
| `timeout_millis` | `int` | `300000` | Max execution time in ms (default 5 minutes) |

### `RemoteSession`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `backend` | `AnyBackendConfig` | required | Backend for the shared sandbox |
| `local_project_root` | `Path` | required | Root directory of the project |
| `timeout_millis` | `int` | `300000` | Default per-call timeout |

Lifecycle API: `await start()`, `async with session:` (activates; owns the sandbox
only if it wasn't started explicitly), `await pause() -> SessionRef`, `session.ref`,
classmethod `await RemoteSession.resume(ref, backend=..., local_project_root=...)`,
`await close()`.

### `E2B` config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `template_prefix` | required | Prefix for E2B template name (`{prefix}-v{version}-{hash}`) |
| `e2b_api_key` | `None` | API key (falls back to `E2B_API_KEY` env var) |
| `template_version` | `None` | Override version; defaults to `pyproject.toml` version |
| `dockerfile_path` | `None` | Path to Dockerfile; defaults to `Dockerfile` in project root |
| `cpu_count` | `1` | CPUs to allocate |
| `memory_mb` | `1024` | Memory in MB to allocate |
| `auto_build_override` | `None` | Pin auto-build for this config, overriding `REMOTE_BOX_AUTO_BUILD`; prefer leaving unset |
| `sandbox_ttl_seconds` | `600` | Sandbox lifetime, refreshed before every call |

### `Daytona` config

| Parameter | Default | Description |
|-----------|---------|-------------|
| `snapshot_name` | required | Prefix for Daytona snapshot name (`{name}-v{version}-{hash}`) |
| `daytona_api_key` | `None` | API key (falls back to `DAYTONA_API_KEY` env var) |
| `snapshot_version` | `None` | Override version; defaults to `pyproject.toml` version |
| `dockerfile_path` | `None` | Path to Dockerfile; defaults to `Dockerfile` in project root |
| `cpu_count` | `1` | CPUs to allocate |
| `memory_gb` | `1` | Memory in GB to allocate |
| `disk_gb` | `3` | Disk in GB to allocate |
| `auto_build_override` | `None` | Pin auto-build for this config, overriding `REMOTE_BOX_AUTO_BUILD`; prefer leaving unset |
| `create_timeout_seconds` | `120` | Max time to wait for sandbox creation |

### `Subprocess` config

No parameters. Runs locally via `bash` + `uv run` (both must be on `PATH`).
