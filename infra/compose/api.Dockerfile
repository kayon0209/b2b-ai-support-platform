FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY requirements.txt ./
COPY apps/api/src ./apps/api/src
COPY apps/worker/src ./apps/worker/src
COPY packages ./packages
COPY apps/api/migrations ./apps/api/migrations

# The manifest, not a hand-written list. This step used to name eleven packages
# inline and had drifted: it omitted `prometheus-client`, which
# `platform_core.main` imports, so the container raised ModuleNotFoundError on
# startup and could never serve a request - while the test suite, which runs
# against the venv, stayed green. A list maintained in two places is a list
# that disagrees with itself.
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e . --no-deps 2>/dev/null || true

ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src:/app/packages/contracts/src:/app/packages/policy/src:/app/packages/observability/src

EXPOSE 8000

# Default entrypoint is the API. The worker service in docker-compose
# overrides `command` to run `python -m worker.runner`, reusing this exact
# image so API and worker cannot drift apart.
CMD ["uvicorn", "platform_core.main:app", "--host", "0.0.0.0", "--port", "8000"]
