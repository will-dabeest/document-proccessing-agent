# Engineering journal

- 2026-05-13: Sprint 1 — Added LocalStack + Jaeger compose, Terraform resources (S3, SQS+DLQ, DynamoDB, Secrets Manager), dev-up and verify scripts.
- 2026-05-13: Sprint 2–6 — FastAPI ingestion (upload, SQS publish with trace attrs, OTel, `/documents`, `/ask`, `/internal/index`), worker (`worker_app`: SQS poll, Dynamo idempotency, LangGraph+Ollama with `LLM_MOCK_JSON` for CI), shared `JobMessage` schema.
- 2026-05-13: Sprint 7–9 — Vite React UI (polling, upload, RAG ask), Chroma + sentence-transformers lazy-loaded in ingestion, CI workflow (Python 3.11, frontend build, Terraform validate, tfsec soft-fail).
- 2026-05-13: Compose-first local dev — extended `infra/docker-compose.yml` (LocalStack healthcheck, Terraform init/apply job, ingestion/worker/frontend builds via `infra/Dockerfile.*`, optional Ollama profile `llm`); `.env.example`, `docs/local-dev.md`, `Taskfile.yml`, CI job `compose-config`, `scripts/dev-up.ps1` uses compose up.