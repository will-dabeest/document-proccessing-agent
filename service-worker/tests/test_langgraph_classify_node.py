"""Unit tests for classify_node and _ollama_generate edge cases (mocked LLM)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import worker_app.langgraph_flow as lg


def test_classify_node_valid_json_first_call():
    raw = '{"classification": "Legal", "summary": "Contract terms."}'
    with patch.object(lg, "_ollama_generate", return_value=raw):
        out = lg.classify_node({"document_text": "Some doc", "attempts": 0})
    assert out["classification"] == "Legal"
    assert out["summary"] == "Contract terms."
    assert out["attempts"] == 0


def test_classify_node_valid_json_preserves_prior_attempts():
    raw = '{"classification": "A", "summary": "B"}'
    with patch.object(lg, "_ollama_generate", return_value=raw):
        out = lg.classify_node({"document_text": "x", "attempts": 5})
    assert out["attempts"] == 5


def test_classify_node_invalid_json_then_valid_json():
    fixed = '{"classification": "Tech", "summary": "Fixed summary."}'
    prompts: list[str] = []

    def fake_generate(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "not valid json at all"
        return fixed

    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node({"document_text": "hello", "attempts": 0})

    assert out["classification"] == "Tech"
    assert out["summary"] == "Fixed summary."
    assert out["attempts"] == 1
    assert len(prompts) == 2
    assert "invalid JSON" in prompts[1]
    assert "not valid json" in prompts[1]


def test_classify_node_invalid_json_twice():
    prompts: list[str] = []

    def fake_generate(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "still not json"

    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node({"document_text": "doc", "attempts": 0})

    assert out["classification"] == "Unknown"
    assert out["summary"] == "still not json"[:500]
    assert out["attempts"] == 2
    assert len(prompts) == 2


def test_classify_node_attempts_ge_two_skips_fix_branch():
    with patch.object(lg, "_ollama_generate", return_value="no json here") as gen:
        out = lg.classify_node({"document_text": "d", "attempts": 2})

    gen.assert_called_once()
    assert out["classification"] == "Unknown"
    assert out["summary"] == "no json here"[:500]
    assert out["attempts"] == 3


def test_classify_node_missing_keys_use_defaults():
    raw = "{}"
    with patch.object(lg, "_ollama_generate", return_value=raw):
        out = lg.classify_node({"document_text": "anything"})
    assert out["classification"] == "General"
    assert out["summary"] == ""


def test_ollama_generate_uses_mock_json_when_set(monkeypatch):
    monkeypatch.setenv("LLM_MOCK_JSON", '{"classification": "M", "summary": "S"}')
    import importlib

    import worker_app.config as cfg

    importlib.reload(cfg)
    import worker_app.langgraph_flow as lg2

    importlib.reload(lg2)
    try:
        out = lg2._ollama_generate("any prompt")
        assert out == '{"classification": "M", "summary": "S"}'
    finally:
        monkeypatch.delenv("LLM_MOCK_JSON", raising=False)
        importlib.reload(cfg)
        importlib.reload(lg)


def test_ollama_generate_http_failure_returns_fallback_json():
    fake_settings = SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://127.0.0.1:9",
        ollama_model="llama3",
    )
    with patch.object(lg, "settings", fake_settings), patch.object(
        lg.httpx, "post", side_effect=ConnectionError("refused")
    ):
        out = lg._ollama_generate("prompt")

    data = lg._parse_json_obj(out)
    assert data["classification"] == "Unknown"
    assert "unavailable" in data["summary"].lower()


def test_classify_node_after_ollama_http_failure_parses_fallback():
    fake_settings = SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://127.0.0.1:9",
        ollama_model="llama3",
    )
    with patch.object(lg, "settings", fake_settings), patch.object(
        lg.httpx, "post", side_effect=ConnectionError("down")
    ):
        out = lg.classify_node({"document_text": "text"})

    assert out["classification"] == "Unknown"
    assert "unavailable" in (out.get("summary") or "").lower()


def test_ollama_generate_posts_to_configured_generate_url():
    fake_settings = SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://ollama.internal:11434",
        ollama_model="llama3:instruct",
        ollama_http_timeout_seconds=12.5,
    )
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": '{"classification":"X","summary":"Y"}'}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200

    with patch.object(lg, "settings", fake_settings), patch.object(
        lg.httpx, "post", return_value=mock_resp
    ) as post:
        out = lg._ollama_generate("prompt-body")

    post.assert_called_once_with(
        "http://ollama.internal:11434/api/generate",
        json={
            "model": "llama3:instruct",
            "prompt": "prompt-body",
            "stream": False,
        },
        timeout=12.5,
    )
    assert out == '{"classification":"X","summary":"Y"}'
