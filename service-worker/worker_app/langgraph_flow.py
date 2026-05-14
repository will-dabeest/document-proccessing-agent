from __future__ import annotations

import json
import logging
import re
import time
from typing import TypedDict

import httpx
from langgraph.graph import END, StateGraph
from opentelemetry import trace

from worker_app.config import settings
from shared.llm_telemetry import apply_ollama_response_to_span, log_llm_event

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class AgentState(TypedDict, total=False):
    document_text: str
    classification: str
    summary: str
    attempts: int
    job_id: str
    s3_key: str


def _ollama_generate(
    prompt: str,
    *,
    stage: str = "worker.classify",
    attempts: int | None = None,
    job_id: str | None = None,
) -> str:
    model = settings.ollama_model
    if settings.llm_mock_json:
        t0 = time.perf_counter()
        with tracer.start_as_current_span("llm.ollama.generate") as span:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            apply_ollama_response_to_span(
                span,
                response_json=None,
                latency_ms=latency_ms,
                prompt_char_len=len(prompt),
                model=model,
                http_status_code=None,
                outcome="mock",
                stage=stage,
                attempts=attempts,
            )
            log_llm_event(
                logger,
                span="llm.ollama.generate",
                outcome="mock",
                model=model,
                latency_ms=round(latency_ms, 3),
                prompt_chars=len(prompt),
                llm_stage=stage,
                job_id=job_id,
                llm_attempts=attempts,
            )
            return settings.llm_mock_json

    t0 = time.perf_counter()
    with tracer.start_as_current_span("llm.ollama.generate") as span:
        try:
            r = httpx.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=120.0,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            try:
                data = r.json()
            except Exception:
                data = None
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                apply_ollama_response_to_span(
                    span,
                    response_json=data if isinstance(data, dict) else None,
                    latency_ms=latency_ms,
                    prompt_char_len=len(prompt),
                    model=model,
                    http_status_code=r.status_code,
                    outcome="http_error",
                    stage=stage,
                    attempts=attempts,
                )
                log_llm_event(
                    logger,
                    span="llm.ollama.generate",
                    outcome="http_error",
                    model=model,
                    latency_ms=round(latency_ms, 3),
                    prompt_chars=len(prompt),
                    http_status_code=r.status_code,
                    llm_stage=stage,
                    job_id=job_id,
                    llm_attempts=attempts,
                )
                logger.warning("ollama_failed: HTTP %s", r.status_code)
                return '{"classification": "Unknown", "summary": "LLM unavailable."}'

            text = ((data or {}).get("response") or "").strip()
            outcome = "ok" if text else "empty_response"
            apply_ollama_response_to_span(
                span,
                response_json=data if isinstance(data, dict) else None,
                latency_ms=latency_ms,
                prompt_char_len=len(prompt),
                model=model,
                http_status_code=r.status_code,
                outcome=outcome,
                stage=stage,
                attempts=attempts,
            )
            log_llm_event(
                logger,
                span="llm.ollama.generate",
                outcome=outcome,
                model=model,
                latency_ms=round(latency_ms, 3),
                prompt_chars=len(prompt),
                http_status_code=r.status_code,
                llm_stage=stage,
                job_id=job_id,
                llm_attempts=attempts,
            )
            return text
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            apply_ollama_response_to_span(
                span,
                response_json=None,
                latency_ms=latency_ms,
                prompt_char_len=len(prompt),
                model=model,
                http_status_code=None,
                outcome="exception",
                stage=stage,
                attempts=attempts,
            )
            log_llm_event(
                logger,
                span="llm.ollama.generate",
                outcome="exception",
                model=model,
                latency_ms=round(latency_ms, 3),
                prompt_chars=len(prompt),
                error=str(e),
                llm_stage=stage,
                job_id=job_id,
                llm_attempts=attempts,
            )
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
    job_id = state.get("job_id")
    s3_key = state.get("s3_key")
    with tracer.start_as_current_span("llm.graph.classify") as gspan:
        gspan.set_attribute("llm.stage", "worker.classify")
        if job_id:
            gspan.set_attribute("job_id", str(job_id))
        if s3_key:
            gspan.set_attribute("s3.key", str(s3_key))
        gspan.set_attribute("llm.attempts", attempts)

        prompt = f"""Classify and summarize the document. Return ONLY valid JSON with keys classification (short string) and summary (one paragraph).

Document:
{doc[:8000]}
"""
        raw = _ollama_generate(
            prompt, stage="worker.classify", attempts=attempts, job_id=str(job_id) if job_id else None
        )
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
            fixed = _ollama_generate(
                fix_prompt,
                stage="worker.classify_json_repair",
                attempts=attempts,
                job_id=str(job_id) if job_id else None,
            )
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
