# Use Python 3.10 slim trixie with UV pre-installed image as base
FROM ghcr.io/astral-sh/uv:python3.10-trixie-slim

# Set working directory
WORKDIR /app

# Only copy dependency files first to maximize cache hits
COPY pyproject.toml ./
COPY uv.lock ./
COPY README.md ./

# Install third-party dependencies only (without the local package) so this
# expensive layer only invalidates when dependencies change, not on code changes
RUN uv sync --no-dev --no-install-project

# Copy the rest of your application code
COPY ./src ./src

# Now install the local package itself (src/remote is now present)
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Run the application
CMD ["python", "main.py"]
