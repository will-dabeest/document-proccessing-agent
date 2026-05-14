# Document processing

Local-first stack for ingesting documents, queuing work to a background worker, and asking questions with optional RAG over indexed content.

## What is in this repo

- **service-ingestion** — FastAPI service: uploads, publishes jobs to SQS, document listing, `/ask` with retrieval when configured, OpenTelemetry traces.
- **service-worker** — Polls SQS, idempotent processing (DynamoDB), LangGraph flows with Ollama (or mocks for CI).
- **frontend** — Vite + React UI for upload, status, and ask.
- **infra** — Docker Compose (LocalStack, Terraform apply job, services, Jaeger), Dockerfiles, Terraform for AWS-shaped resources on LocalStack.
- **shared** — Schemas and shared utilities used by ingestion and worker.
- **Architecture** — Diagrams and flows for new developers: [docs/architecture.md](docs/architecture.md).

Optional **LLM / RAG** paths use Ollama and Chroma.

At a high level, the **browser** talks to the **ingestion** API through the Vite dev proxy (`/api` → FastAPI on port 8000 in the default Compose layout). **Uploads** are stored in **S3** and a message is sent to **SQS**; the **worker** consumes the queue, reads the object from S3, writes **DynamoDB** status, runs **LangGraph** over extracted text, and may **POST** back to ingestion to **index** chunks into **Chroma** for **`/ask`**. Operational detail and ports live in [docs/local-dev.md](docs/local-dev.md).

## Local development

**[docs/local-dev.md](docs/local-dev.md)** is the single place for local setup: Docker prerequisites, running the stack (including the **ollama** service by default), verifying services, tests on the host, teardown, optional `.env`, LLM/RAG and Ollama, hot reload on the host, Jaeger-oriented notes, and troubleshooting.

## Project history and notes

Sprint-level changes and technical context: [docs/engineering-journal.md](docs/engineering-journal.md).

## Developing with Cursor

Contributors using Cursor follow: **Plan → Generate → Test → Evaluate → Improve**. Project rules live under [`.cursor/rules/`](.cursor/rules/) (`.mdc` files with YAML frontmatter).

For automations, plugins, diagrams, and rule maintenance in one place, see [docs/cursor-agent-workflow.md](docs/cursor-agent-workflow.md).
