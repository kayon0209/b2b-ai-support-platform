FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY apps/api/src ./apps/api/src
COPY apps/worker/src ./apps/worker/src
COPY packages ./packages
COPY apps/api/migrations ./apps/api/migrations

RUN pip install --no-cache-dir fastapi 'uvicorn[standard]' sqlalchemy[asyncio] 'psycopg[binary]' pydantic pydantic-settings uuid6 alembic httpx jsonschema && \
    pip install --no-cache-dir -e . --no-deps 2>/dev/null || true

ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src:/app/packages/contracts/src:/app/packages/policy/src:/app/packages/observability/src

EXPOSE 8000

# Default entrypoint is the API. The worker service in docker-compose
# overrides `command` to run `python -m worker.runner`, reusing this exact
# image so API and worker cannot drift apart.
CMD ["uvicorn", "platform_core.main:app", "--host", "0.0.0.0", "--port", "8000"]
