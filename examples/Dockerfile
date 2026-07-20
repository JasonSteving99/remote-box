# Sandbox image for the examples — no pyproject.toml required. The sandbox
# just needs remote-box importable at /app/.venv/bin/python and the example
# modules on the import path (WORKDIR is the import root).
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

WORKDIR /app
RUN uv venv && uv pip install remote-box
ENV PATH="/app/.venv/bin:$PATH"

COPY . .
