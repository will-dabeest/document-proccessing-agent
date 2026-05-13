# Quickstart: Using Cursor in This Repo

This project is wired for agentic development with a minimal, universal setup.

## 1. Core Mental Model

Everything runs through this loop:

> Plan → Generate → Test → Evaluate → Improve

## 2. Common Tasks

### Implement a Feature
Use the “Implement a Feature” prompt from cursor-prompts.md.

### Fix a Bug
Use the “Fix a Bug” prompt.

### Add Tests
Use the “Add Tests” prompt.

## 3. Automations

Recommended Cursor Automations:

- Fix CI Failures
- Add Test Coverage
- Assign PR Reviewers (optional)

## 4. Plugins

Recommended plugins:

- Sourcegraph
- Aikido
- Sonatype

## 5. Config Maintenance

If any cursor*.md file drifts or breaks, ask Cursor:

> Use the Config Maintenance workflow to apply the Self‑Correction Rule.

## 6. Document platform (local run)

See [docs/local-dev.md](docs/local-dev.md) for Docker Compose (recommended) or host-based scripts.
