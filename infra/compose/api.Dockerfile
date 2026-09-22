FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY requirements.txt ./

# Dependencies are installed BEFORE the source is copied. They change far
# less often than the source, and with the old ordering a one-line edit
# invalidated this layer and re-ran the whole install - measured at 22m54s
# per rebuild, which made iterating on the platform impossible. Now a source
# edit only re-runs the two COPY layers below (seconds), and this layer is
# reused until requirements.txt actually changes.
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/api/src ./apps/api/src
COPY apps/worker/src ./apps/worker/src
COPY packages ./packages
COPY apps/api/migrations ./apps/api/migrations

# The editable install stays after the source COPY because it needs the
# package layout present to build against. It is cheap next to the step above
# and is allowed to fail (`|| true`) - PYTHONPATH below is what actually puts
# the sources on the path.
RUN pip install --no-cache-dir -e . --no-deps 2>/dev/null || true

ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src:/app/packages/contracts/src:/app/packages/policy/src:/app/packages/observability/src

EXPOSE 8000

# Default entrypoint is the API. The worker service in docker-compose
# overrides `command` to run `python -m worker.runner`, reusing this exact
# image so API and worker cannot drift apart.
CMD ["uvicorn", "platform_core.main:app", "--host", "0.0.0.0", "--port", "8000"]
