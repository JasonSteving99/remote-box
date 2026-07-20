# Sandbox image: dependencies come from pyproject.toml, so the sandbox gets
# remote-box AND duckdb — while the local project never installs duckdb at all.
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

WORKDIR /app
COPY pyproject.toml ./
RUN uv venv && uv pip install -r pyproject.toml
ENV PATH="/app/.venv/bin:$PATH"

COPY . .
