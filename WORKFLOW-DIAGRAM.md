# Agentic Workflow Diagram

## High-Level Flow

User Intent
   ↓
[Agent Plans]
   ↓
[Agent Generates Code]
   ↓
[Agent Runs Tests]
   ↓
[Agent Evaluates Results]
   ↓
[Agent Improves]
   ↓
Result

---

## Layered View

[User]
  ↓
[Orchestration Layer]
  ↓
[Agent Layer]
  ↓
[Automations Layer]
  ↓
[Plugins Layer]
  ↓
[Repo]

---

## Config Self-Correction Flow

Config Drift
   ↓
[Config Guardian Agent]
   ↓
Plan → Generate → Test → Evaluate → Improve
   ↓
Config Stable
