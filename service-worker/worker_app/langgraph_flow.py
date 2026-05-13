from __future__ import annotations

import json
import logging
import re
from typing import TypedDict

import httpx
from langgraph.graph import END, StateGraph

from worker_app.config import settings

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    document_text: str
    classification: str
    summary: str
    attempts: int
    job_id: str
    s3_key: str


def _ollama_generate(prompt: str) -> str:
    if settings.llm_mock_json:
        return settings.llm_mock_json
    try:
        r = httpx.post(
            f"{settings.ollama_base_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
            },
            timeout=120.0,
        )
        r.raise_for_status()
        return (r.json().get("response") or "").strip()
    except Exception as e:
        logger.warning("ollama_failed: %s", e)
        return '{"classification": "Unknown", "summary": "LLM unavailable."}'


def _parse_json_obj(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    return json.loads(text)


def classify_node(state: AgentState) -> AgentState:
    attempts = int(state.get("attempts") or 0)
    doc = state.get("document_text") or ""
    prompt = f"""Classify and summarize the document. Return ONLY valid JSON with keys classification (short string) and summary (one paragraph).

Document:
{doc[:8000]}
"""
    raw = _ollama_generate(prompt)
    try:
        data = _parse_json_obj(raw)
        return {
            **state,
            "classification": str(data.get("classification", "General")),
            "summary": str(data.get("summary", "")),
            "attempts": attempts,
        }
    except json.JSONDecodeError:
        if attempts >= 2:
            return {
                **state,
                "classification": "Unknown",
                "summary": raw[:500],
                "attempts": attempts + 1,
            }
        fix_prompt = f"""
You returned invalid JSON. Return valid JSON with this schema:
{{
  "classification": "string",
  "summary": "string"
}}

Previous output:
{raw}
"""
        fixed = _ollama_generate(fix_prompt)
        try:
            data = _parse_json_obj(fixed)
            return {
                **state,
                "classification": str(data.get("classification", "General")),
                "summary": str(data.get("summary", "")),
                "attempts": attempts + 1,
            }
        except json.JSONDecodeError:
            return {
                **state,
                "classification": "Unknown",
                "summary": fixed[:500],
                "attempts": attempts + 2,
            }


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("classify", classify_node)
    g.set_entry_point("classify")
    g.add_edge("classify", END)
    return g.compile()


_graph = build_graph()


def run_agent(state: AgentState) -> AgentState:
    return _graph.invoke(state)
