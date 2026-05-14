from __future__ import annotations

import logging
import time

import httpx
from opentelemetry import trace

from app.config import settings
from shared.llm_telemetry import apply_ollama_response_to_span, log_llm_event

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def generate_llama3(prompt: str) -> str:
    """Call Ollama /api/generate using settings.ollama_model. Returns a user-visible string on failure."""
    model = settings.ollama_model
    t0 = time.perf_counter()
    with tracer.start_as_current_span("llm.ollama.generate") as span:
        span.set_attribute("llm.stage", "ingestion.rag")
        try:
            r = httpx.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=settings.ollama_http_timeout_seconds,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            data: dict | None
            try:
                data = r.json()
            except Exception:
                data = None
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                apply_ollama_response_to_span(
                    span,
                    response_json=data,
                    latency_ms=latency_ms,
                    prompt_char_len=len(prompt),
                    model=model,
                    http_status_code=r.status_code,
                    outcome="http_error",
                    stage="ingestion.rag",
                )
                err = (data or {}).get("error") if isinstance(data, dict) else None
                log_llm_event(
                    logger,
                    stage="ingestion.rag",
                    span="llm.ollama.generate",
                    outcome="http_error",
                    model=model,
                    latency_ms=round(latency_ms, 3),
                    prompt_chars=len(prompt),
                    http_status_code=r.status_code,
                    error=err,
                )
                logger.warning(
                    "ollama_generate_failed: HTTP %s model=%s detail=%s",
                    r.status_code,
                    model,
                    err or "",
                )
                return "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
            text = (data.get("response") if data else None) or ""
            text = str(text).strip() or "No answer returned."
            outcome = "ok" if text != "No answer returned." else "empty_response"
            apply_ollama_response_to_span(
                span,
                response_json=data,
                latency_ms=latency_ms,
                prompt_char_len=len(prompt),
                model=model,
                http_status_code=r.status_code,
                outcome=outcome,
                stage="ingestion.rag",
            )
            log_llm_event(
                logger,
                stage="ingestion.rag",
                span="llm.ollama.generate",
                outcome=outcome,
                model=model,
                latency_ms=round(latency_ms, 3),
                prompt_chars=len(prompt),
                http_status_code=r.status_code,
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
                stage="ingestion.rag",
            )
            log_llm_event(
                logger,
                stage="ingestion.rag",
                span="llm.ollama.generate",
                outcome="exception",
                model=model,
                latency_ms=round(latency_ms, 3),
                prompt_chars=len(prompt),
                error=str(e),
            )
            logger.warning("ollama_generate_failed: %s", e)
            return "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
