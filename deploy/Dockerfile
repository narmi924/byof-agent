FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.12.6-slim-bookworm AS dependencies

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python

FROM dependencies AS test-dependencies
RUN uv sync --locked --no-install-project --python /usr/local/bin/python

FROM python:3.12.6-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 byof && useradd --uid 10001 --gid byof --no-create-home byof
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY packages/ packages/
COPY services/ services/
COPY scripts/ scripts/
COPY database/ database/
COPY data/development/ data/development/
COPY alembic.ini pyproject.toml uv.lock ./
USER 10001:10001
CMD ["python", "-m", "uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS tests
COPY --from=test-dependencies /app/.venv /app/.venv
COPY . /app
ENV PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache" MYPY_CACHE_DIR=/tmp/mypy-cache RUFF_CACHE_DIR=/tmp/ruff-cache
CMD ["python", "-m", "scripts.container_checks"]

FROM runtime AS final
