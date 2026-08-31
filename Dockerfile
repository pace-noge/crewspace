# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.14-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 crewspace \
    && useradd --uid 10001 --gid crewspace --create-home --no-log-init crewspace \
    && mkdir -p /app/data \
    && chown -R crewspace:crewspace /app
COPY --from=builder --chown=crewspace:crewspace /app/.venv /app/.venv
COPY --chown=crewspace:crewspace alembic.ini ./alembic.ini
COPY --chown=crewspace:crewspace migrations ./migrations
COPY --chown=crewspace:crewspace src ./src
USER crewspace
EXPOSE 8000
CMD ["uvicorn", "crewspace.main:app", "--host", "0.0.0.0", "--port", "8000"]
