# DuckDB example: sandbox-only dependencies

Demonstrates that a `@remote` function's dependencies live in the **sandbox
image**, not your local environment. Here, `duckdb` is never installed locally —
yet the example creates a `.duckdb` database and runs SQL against it remotely.

## How it works

1. **`pyproject.toml` declares the sandbox's deps** (`remote-box`, `duckdb`).
   The `Dockerfile` installs them into the image with
   `uv pip install -r pyproject.toml`. Your local project never installs them.
   (The `version` field is ignored by remote-box — image names come from
   hashing this directory's contents, so editing any file here automatically
   triggers a rebuild.)

2. **Sandbox-only imports go inside the function body.** `main.py` must import
   cleanly on your machine (that's how remote-box discovers and serializes the
   calls), so `import duckdb` happens inside the `@remote` functions, which
   only ever execute in the sandbox.

3. **A `RemoteSession` keeps one sandbox across calls.** `load_trips` writes
   `/tmp/trips.duckdb` in the sandbox; `run_query` reads it in a second call —
   same filesystem, because both calls run in the session's sandbox. Without
   the session, each call would get a fresh sandbox and the file would be gone.

Only Pydantic models cross the wire (as JSON), so the local side needs no
knowledge of DuckDB at all — that separation is a big part of why you'd reach
for a sandbox in the first place.

## Run it

One-time setup: at the **repo root**, copy `.env.example` to `.env.local` and
fill in the API keys for the backends you use — the justfile loads it
automatically.

```bash
cp .env.example .env.local   # at the repo root
```

Then, from this directory:

```bash
just run            # Daytona (default)
just run e2b        # E2B
```

(Or build the image without running: `uv run remote-box build examples/duckdb/`.)
