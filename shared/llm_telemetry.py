"""Shared helpers for LLM/RAG OpenTelemetry spans and structured logs (ingestion + worker)."""

from __future__ import annotations

import json
import logging
from statistics import mean
from typing import Any, Mapping

# Ollama /api/generate JSON fields we surface when present (names vary by version).
_OLLAMA_USAGE_NUMERIC_KEYS = frozenset(
    {
        "prompt_eval_count",
        "eval_count",
        "total_duration",
        "load_duration",
        "prompt_eval_duration",
        "eval_duration",
    }
)


def log_llm_event(logger: logging.Logger, **fields: Any) -> None:
    """Single-line JSON log for grep and log aggregators."""
    payload = {k: v for k, v in sorted(fields.items()) if v is not None}
    logger.info("llm_event %s", json.dumps(payload, default=str))


def rag_distance_stats(distances: list[float] | None) -> tuple[float | None, float | None, float | None]:
    """Return (min, max, mean) for Chroma distance rows, or Nones if empty."""
    if not distances:
        return None, None, None
    return min(distances), max(distances), mean(distances)


def flatten_distances(raw: Any) -> list[float]:
    """Normalize Chroma distances (often list[list[float]]) to a flat list of floats."""
    if raw is None:
        return []
    if isinstance(raw, list) and raw and isinstance(raw[0], list):
        inner = raw[0]
    else:
        inner = raw if isinstance(raw, list) else []
    out: list[float] = []
    for x in inner:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def ollama_usage_span_attributes(data: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Map Ollama response JSON to span attribute keys (numeric only)."""
    if not data:
        return {}
    out: dict[str, int | float] = {}
    for key in _OLLAMA_USAGE_NUMERIC_KEYS:
        val = data.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            out[f"llm.usage.{key}"] = val
    return out


def apply_ollama_response_to_span(
    span: Any,
    *,
    response_json: Mapping[str, Any] | None,
    latency_ms: float,
    prompt_char_len: int,
    model: str | None,
    http_status_code: int | None,
    outcome: str,
    stage: str | None = None,
    context_char_len: int | None = None,
    rag_n_requested: int | None = None,
    rag_n_returned: int | None = None,
    rag_empty_retrieval: bool | None = None,
    rag_distance_min: float | None = None,
    rag_distance_max: float | None = None,
    rag_distance_mean: float | None = None,
    attempts: int | None = None,
) -> None:
    """Set standard GenAI / LLM / RAG attributes on a span (no raw prompt text)."""
    span.set_attribute("gen_ai.system", "ollama")
    if model:
        span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("llm.latency_ms", round(float(latency_ms), 3))
    span.set_attribute("llm.prompt_chars", int(prompt_char_len))
    span.set_attribute("llm.outcome", outcome)
    if stage is not None:
        span.set_attribute("llm.stage", stage)
    if http_status_code is not None:
        span.set_attribute("http.status_code", int(http_status_code))
    if context_char_len is not None:
        span.set_attribute("llm.context_chars", int(context_char_len))
    if rag_n_requested is not None:
        span.set_attribute("rag.n_results_requested", int(rag_n_requested))
    if rag_n_returned is not None:
        span.set_attribute("rag.n_chunks_returned", int(rag_n_returned))
    if rag_empty_retrieval is not None:
        span.set_attribute("rag.empty_retrieval", bool(rag_empty_retrieval))
    if rag_distance_min is not None:
        span.set_attribute("rag.distance_min", float(rag_distance_min))
    if rag_distance_max is not None:
        span.set_attribute("rag.distance_max", float(rag_distance_max))
    if rag_distance_mean is not None:
        span.set_attribute("rag.distance_mean", float(rag_distance_mean))
    if attempts is not None:
        span.set_attribute("llm.attempts", int(attempts))
    for k, v in ollama_usage_span_attributes(dict(response_json) if response_json else None).items():
        span.set_attribute(k, v)
