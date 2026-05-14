# Local development (Compose-first)

The [README](../README.md) points here for **all** local commands, URLs, and troubleshooting—keep this file updated when those details change.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose **v2.20+** — required for `service_completed_successfully` conditions in Compose)
- **Python 3.11+** if you run tests or services on the host (same major as CI)
- **Node 20+** if you work on the frontend outside Compose

## Quick start (full stack)

LocalStack is pinned to a specific Docker image tag in Compose (not `latest`) so local dev does not depend on `LOCALSTACK_AUTH_TOKEN` or other LocalStack Cloud licensing that newer unified images may require.

**Docker Compose:** from the **repository root**, run `cd infra`, then use the `docker compose …` commands below. Compose resolves paths in [docker-compose.yml](../infra/docker-compose.yml) relative to that folder.

```bash
cd infra
docker compose up --build
```

This starts, in order:

1. **LocalStack** (waits until healthy)
2. **terraform** (one-shot `init` + `apply` against `http://localstack:4566`)
3. **ingestion** (FastAPI on [http://localhost:8000](http://localhost:8000)), **worker**, **frontend** (Vite on [http://localhost:5173](http://localhost:5173))
4. **Jaeger** UI on [http://localhost:16686](http://localhost:16686)
5. **Ollama** ([http://localhost:11434](http://localhost:11434) from the host). Ingestion and the worker use `OLLAMA_BASE_URL=http://ollama:11434` as set in [docker-compose.yml](../infra/docker-compose.yml). Pull a model once inside the container (see below).

### Verify

| Check | URL |
|-------|-----|
| Ingestion health | [http://localhost:8000/health](http://localhost:8000/health) |
| Frontend | [http://localhost:5173](http://localhost:5173) |
| Jaeger | [http://localhost:16686](http://localhost:16686) |

### Common commands

From `infra/` (after `cd infra`):

```bash
# Stop and remove containers
docker compose down

# Follow logs
docker compose logs -f

# Validate compose file (CI runs the same check)
docker compose config --quiet
```

### Python tests (host)

From the repository root (after `pip install -r requirements.txt`):

```bash
python -m pytest -q
```

### Optional: `.env`

Copy [.env.example](.env.example) to `.env` at the repo root if you need overrides. Compose loads `.env` when present (`required: false`).

### Ollama in Compose (default)

`docker compose up` starts the **ollama** service with the rest of the stack. Models live in the `ollama_data` volume (not your host Ollama install). After the first `up`, pull the model the apps expect (`OLLAMA_MODEL` in `.env`, default `llama3:latest`):

```bash
docker compose exec ollama ollama pull llama3:latest
```

**Port 11434:** the Compose **ollama** container publishes `11434` on the host. Stop any **host** Ollama app that is already using that port, or Docker will fail to bind it.

**Host Ollama instead of the container:** the compose file sets `OLLAMA_BASE_URL=http://ollama:11434` on **ingestion** and **worker** (overrides repo-root `.env` for those keys). To call Ollama on the host while other services stay in Docker, use a compose override or edit that URL—for example `http://host.docker.internal:11434` and omit or stop the **ollama** service so ports do not clash. On **Linux** Docker Engine, `extra_hosts` in the compose file maps `host.docker.internal` via `host-gateway`.

### LLM and RAG traces in Jaeger

After you call `POST /ask` on ingestion or process a document with the worker, open the Jaeger UI and search traces for **`service-ingestion`** or **`service-worker`**. Child spans include:

- **`rag.retrieve_context`**, **`rag.embed_query`**, **`rag.chroma.query`** — retrieval latency, chunk counts, optional distance summary on the parent retrieve span.
- **`llm.ollama.generate`** — model name, prompt length, latency, HTTP status on errors, and Ollama usage fields when the API returns them (`prompt_eval_count`, `eval_count`, etc.).
- **`llm.graph.classify`** (worker) — one graph-level span around classification.

Structured **`llm_event`** JSON lines also appear in service logs for grep-friendly monitoring.

## Advanced: hot reload on the host

Use this when you want `--reload` on FastAPI or faster frontend iteration while still using LocalStack and Jaeger in Docker.

1. **Infra only** — from `infra/` (same `cd infra` convention as Quick start):

   ```bash
   docker compose up -d localstack jaeger
   ```

   Wait until LocalStack is healthy (Compose marks the service healthy, or open [http://localhost:4566/_localstack/health](http://localhost:4566/_localstack/health)).

2. **Apply Terraform once** (creates S3, SQS, DynamoDB, Secrets Manager on LocalStack):

   ```bash
   docker compose run --rm terraform
   ```

3. **Environment** — copy [.env.example](.env.example) to `.env` so `AWS_ENDPOINT_URL`, `QUEUE_URL`, `INGESTION_BASE_URL`, and `OTLP_ENDPOINT` point at `localhost` (see comments in `.env.example`).

4. **Run services** (separate terminals, from repo root):

   - **Ingestion:** set `PYTHONPATH` to the repository root, `cd service-ingestion`, then `python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`.
   - **Worker:** set `PYTHONPATH` to the repository root, `cd service-worker`, then `python -m worker_app.worker`.
   - **Frontend:** `cd frontend && npm install && npm run dev`.

## Mermaid diagram viewer

If you edit Mermaid code fences in [architecture.md](architecture.md) or [cursor-agent-workflow.md](cursor-agent-workflow.md), regenerate the static HTML viewer from the **repository root**:

```bash
python scripts/generate_diagram_viewer.py
```

Then open [diagrams-viewer.html](diagrams-viewer.html) in a browser. CI fails if the committed viewer is out of date relative to those Markdown files.

**Cursor (Agent):** [`.cursor/hooks.json`](../.cursor/hooks.json) runs `afterFileEdit` (Agent **Write** only) → [`scripts/cursor_mermaid_after_file_edit.py`](../scripts/cursor_mermaid_after_file_edit.py), which calls the same generator when one of those two Markdown files is written by the agent. It does **not** run on ordinary manual saves in the editor; if you edit diagrams by hand, run the command above (or let CI remind you on the next PR).

## Troubleshooting

- **UI shows “LLM unavailable; install Ollama…”** — The **ingestion** container cannot reach the Ollama API or the model is missing. With the default stack, run `docker compose exec ollama ollama pull llama3:latest` (see *Ollama in Compose (default)* above). If you pointed ingestion at **host** Ollama instead, use `http://host.docker.internal:11434` and ensure nothing else binds host port **11434** if the Compose **ollama** service is also enabled.
- **Worker logs `timed out` on `llm.ollama.generate`** — Large prompts or CPU-only inference can exceed the HTTP client limit. Increase **`OLLAMA_HTTP_TIMEOUT_SECONDS`** in `.env` (default **600** in code after a rebuild) and restart **worker** and **ingestion**.
- **First `up --build` is slow** — Docker is pulling images and building service images; later runs are faster.
- **Port already in use** (4566, 8000, 5173, …) — stop the other process or change host ports in `infra/docker-compose.yml` (not recommended unless you know the follow-on env changes).
- **Terraform or ingestion fails** — from `infra/`, `docker compose logs terraform` or `docker compose logs ingestion` for the failing service.
- **Compose version errors** — upgrade to Docker Compose v2.20 or newer.
