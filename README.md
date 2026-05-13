# Agentic Development with Cursor

This repo is configured for minimal, universal, gold‑standard agentic workflows using Cursor.

Core design:

- Agentic loop: Plan → Generate → Test → Evaluate → Improve
- Minimalism: No bloat, no vendor lock‑in, no unnecessary rules
- Universality: Works across languages, stacks, and project types
- Safety: Security, dependency hygiene, and test‑driven iteration
- Self‑healing: Config can be minimally auto‑corrected when inconsistent

Configuration is split into modular files:

- cursor.md
- cursor-rules.md
- cursor-workflows.md
- cursor-orchestration.md
- cursor-prompts.md
- cursor-agents.md
- cursor-automations.md
- cursor-plugins.md
- cursor-architecture.md
- cursor-extensions.md

## Core Loop

All agents follow:

> Plan → Generate → Test → Evaluate → Improve

See cursor-architecture.md for the full system view.

## Document processing stack

Run the API, worker, frontend, LocalStack, and Jaeger locally: see [docs/local-dev.md](docs/local-dev.md).
