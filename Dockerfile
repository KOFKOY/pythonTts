FROM ghcr.io/astral-sh/uv:python3.12-alpine

WORKDIR /app

# Preinstall dependencies (without project files) to leverage Docker layer cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --compile-bytecode --python-preference only-managed

# Copy application code and install the project itself
COPY . .
RUN uv sync --frozen --compile-bytecode

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
