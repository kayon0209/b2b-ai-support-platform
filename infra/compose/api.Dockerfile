# The built frontend, produced in its own stage.
#
# It is a separate stage rather than an `apt install nodejs` in the runtime
# image for two reasons: the final image carries no Node toolchain to patch,
# and the build cache for `npm ci` is keyed on `package-lock.json` alone, so
# editing Python source does not re-run it.
#
# The output lands where `platform_core.spa` looks by default
# (`/app/apps/admin-web/dist`), which is what makes the product shippable from
# one image: before this, `GET /support` answered 401 and nothing in the
# repository described how the interface was deployed at all.
FROM node:22-slim AS frontend

WORKDIR /web
# Manifests first, so `npm ci` is cached until a dependency actually changes.
COPY apps/admin-web/package.json apps/admin-web/package-lock.json ./
RUN npm ci
COPY apps/admin-web/ ./
# `build` runs `tsc -b && vite build`, so a type error fails the image rather
# than shipping a bundle the browser cannot execute.
RUN npm run build


FROM python:3.12-slim

WORKDIR /app

# PostgreSQL client binaries. Needed by the backup CronJob (`pg_dump`) and by
# `scripts/backup_restore_drill.py` (`pg_dump`/`pg_restore`/`psql`).
#
# They live in *this* image rather than a dedicated backup image so the restore
# drill runs against the same artifacts that serve traffic. A drill performed
# with different tooling is a different recovery path from the one that would
# actually be used at 3am, and it would report on the wrong one.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

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
# Operational scripts travel with the image because the backup CronJob and the
# restore drill are invoked from it. Without this they would run a version
# pinned in a ConfigMap, and the drill would stop testing what is deployed.
COPY scripts ./scripts

# The editable install stays after the source COPY because it needs the
# package layout present to build against. It is cheap next to the step above
# and is allowed to fail (`|| true`) - PYTHONPATH below is what actually puts
# the sources on the path.
RUN pip install --no-cache-dir -e . --no-deps 2>/dev/null || true

# The only thing the API needs from the frontend is the built directory. The
# source is not copied: it is not served, not indexed, and shipping it would
# put the whole React tree in the image for nothing.
COPY --from=frontend /web/dist ./apps/admin-web/dist

ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src:/app/packages/contracts/src:/app/packages/policy/src:/app/packages/observability/src

EXPOSE 8000

# Default entrypoint is the API. The worker service in docker-compose
# overrides `command` to run `python -m worker.runner`, reusing this exact
# image so API and worker cannot drift apart.
CMD ["uvicorn", "platform_core.main:app", "--host", "0.0.0.0", "--port", "8000"]
