# Local development (Compose-first)

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose v2.20+ for `service_completed_successfully` health conditions)
- Optional: [Task](https://taskfile.dev/) for cross-platform shortcuts (`task dev`)

## Primary path: full stack in Docker

From the **repository root**:

```bash
docker compose -f infra/docker-compose.yml up --build
```

This starts, in order:

1. **LocalStack** (waits until healthy)
2. **terraform** (one-shot `init` + `apply` against `http://localstack:4566`)
3. **ingestion** (FastAPI on [http://localhost:8000](http://localhost:8000)), **worker**, **frontend** (Vite on [http://localhost:5173](http://localhost:5173))
4. **Jaeger** UI on [http://localhost:16686](http://localhost:16686)

Optional overrides: copy [.env.example](.env.example) to `.env` at the repo root. Compose loads `.env` when present (`required: false` so a missing file is OK).

### Optional: Ollama in Compose

```bash
docker compose -f infra/docker-compose.yml --profile llm up --build
```

Then set `OLLAMA_BASE_URL=http://ollama:11434` in `.env` (or rely on host Ollama at `http://localhost:11434` from the browser’s perspective only for host-run services).

### Tear down

```bash
docker compose -f infra/docker-compose.yml down
```

## Secondary path: hot reload on the host

Use [scripts/dev-up.ps1](scripts/dev-up.ps1) for LocalStack + Terraform only, then:

- [scripts/run_ingestion.ps1](scripts/run_ingestion.ps1)
- [scripts/run_worker.ps1](scripts/run_worker.ps1)
- `cd frontend && npm run dev`

Point `AWS_ENDPOINT_URL`, `QUEUE_URL`, and `INGESTION_BASE_URL` at `http://localhost:4566` and `http://127.0.0.1:8000` respectively (see `.env.example`).

## Task shortcuts

| Task    | Command |
|---------|---------|
| Full up | `task dev` |
| Down    | `task down` |
| Tests   | `task test` |
