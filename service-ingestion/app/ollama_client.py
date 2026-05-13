from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def generate_llama3(prompt: str) -> str:
    """Call Ollama generate API (llama3). Returns a user-visible string on failure."""
    try:
        r = httpx.post(
            f"{settings.ollama_base_url}/api/generate",
            json={"model": "llama3", "prompt": prompt, "stream": False},
            timeout=120.0,
        )
        r.raise_for_status()
        data = r.json()
        return (data.get("response") or "").strip() or "No answer returned."
    except Exception as e:
        logger.warning("ollama_generate_failed: %s", e)
        return "LLM unavailable; install Ollama and pull llama3 for full answers."
