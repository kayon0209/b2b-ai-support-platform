# Project Memory — B2B AI Customer Support Platform

Long-term conventions and environment facts. Daily logs live in `YYYY-MM-DD.md`.

## Environment

- **Working dir**: `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`
- **Python**: `.venv/Scripts/python.exe` (3.12.0, system-based). Managed runtime
  `C:/Users/Rose/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe` has
  **no pytest** — always use the venv.
- **PYTHONPATH must use absolute paths joined by `;` on Windows**:
  ```bash
  R="D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan"
  export PYTHONPATH="$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src"
  ./.venv/Scripts/python.exe -m pytest apps packages -q
  ```
  Using `:` or relative paths yields "No module named 'platform_core'" and makes
  it look like every test file failed to collect.
- **Docker ports**: ai-postgres `5435`, chatwoot-postgres `5434`, ai-redis `6380`,
  chatwoot-redis `6381`, Chatwoot `3000`, API `8000`, Keycloak `8081`, MinIO `9000/9001`.
- **Two DB roles**: `platform` (superuser, bypasses RLS — use for seeding/cleanup)
  and `platform_app` (NOBYPASSRLS — use for anything asserting isolation).
- `packages/observability/src/observability.py` is a top-level `observability`
  module, **not** `platform_core.observability`. Import as
  `from observability import JsonLogger, TraceContext, new_trace_context`.

## Non-obvious correctness rules

- **`set_config('app.tenant_id', ..., true)` is transaction-scoped.** It does not
  survive `COMMIT`. Any read after a commit must re-apply it, or RLS silently
  returns zero rows and you get `NoResultFound` — which looks exactly like
  "the data was never written". This has cost real debugging time.
- **ProviderRole is a StrEnum.** Serialize chat roles with `str(m.role)`, not
  `m.role.value`; the latter breaks when a plain equivalent string is passed.
- **pytest's summary line goes to stderr.** `| grep passed` in a pipeline can
  come back empty even on a fully green run. Trust the **exit code** (0 = all pass).
- **mypy "Duplicate module named `__main__`"** means there are empty
  `src/__init__.py` files making each `src/` look like a package root. The fix is
  `explicit_package_bases = true` plus removing the empty inits — not changing code.
- Celery/Redis for the custom platform must stay **separate** from Chatwoot's Redis.

## LLM provider

- Gitee AI (模力方舟), OpenAI-compatible, `https://ai.gitee.com/v1`.
- Models: `qwen3.8-flash` (chat, reasoning — thinking channel is separated from
  the answer), `Qwen3-Embedding-8B`, `bge-reranker-v2-m3`.
- **`Qwen3-Embedding-8B` is natively 1024-dim but honors `dimensions: 1536`**, so
  the existing `chunks.embedding vector(1536)` column needs no migration.
- Credentials live in `.env` (gitignored). `.env.example` is the tracked template.
  Never commit the real key; unset key means the model boundary fails closed.

## Verification commands

```bash
./.venv/Scripts/python.exe -m pytest apps packages -q -W ignore   # expect exit 0
./.venv/Scripts/python.exe -m ruff check apps packages scripts
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts
./.venv/Scripts/python.exe scripts/smoke_gitee_ai.py             # live provider check
docker compose -f infra/compose/docker-compose.yml config --quiet
```

## Delivery conventions

- Commits: conventional-commit subject, then a body explaining **why**. State the
  test count delta and the ruff/mypy status.
- New domain code must be mypy-strict clean. There are ~49 pre-existing errors in
  older modules; do not add to them and do not fix them incidentally.
- Architecture rules and prohibited shortcuts are in `AGENTS.md` — read it before
  changing module boundaries.
