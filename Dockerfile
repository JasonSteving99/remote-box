# Use Python 3.10 slim trixie with UV pre-installed image as base
FROM ghcr.io/astral-sh/uv:python3.10-trixie-slim

# Set working directory
WORKDIR /app

# Copy dependency files first for better layer caching
COPY . .

# Install dependencies using UV
# --frozen ensures uv.lock is used without modification
# --no-dev skips development dependencies
RUN uv sync --frozen --no-dev

# Set the Python path to use the UV-managed virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Run the application
CMD ["python", "main.py"]
