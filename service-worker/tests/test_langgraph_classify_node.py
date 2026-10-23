"""Unit tests for classify_node and _ollama_generate edge cases (mocked LLM)."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

import worker_app.langgraph_flow as lg

_PROMPT = "PROMPT_MARKER_7c2e_private_clause"


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def __enter__(self) -> "_RecordingSpan":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _worker_settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://127.0.0.1:9",
        ollama_model="llama3:latest",
        ollama_http_timeout_seconds=30.0,
    )


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:9/api/generate")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("server error", request=request, response=response)


def _llm_events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    events = []
    for rec in caplog.records:
        message = rec.getMessage()
        if message.startswith("llm_event "):
            events.append(json.loads(message.removeprefix("llm_event ")))
    return events


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


def test_ollama_generate_http_error_keeps_zero_usage_and_omits_prompt(caplog):
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.json.return_value = {
        "error": "model runner overloaded",
        "prompt_eval_count": 12,
        "eval_count": 0,
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_duration": 4,
        "eval_duration": 0,
    }
    mock_resp.raise_for_status.side_effect = _http_status_error(503)
    span = _RecordingSpan()
    with caplog.at_level(logging.INFO), patch.object(
        lg, "settings", _worker_settings()
    ), patch.object(lg.httpx, "post", return_value=mock_resp) as post, patch.object(
        lg.tracer, "start_as_current_span", return_value=span
    ):
        out = lg._ollama_generate(_PROMPT, attempts=1, job_id="job-7c2e")

    parsed = lg._parse_json_obj(out)
    assert parsed["classification"] == "Unknown"
    assert "unavailable" in parsed["summary"].lower()
    assert post.call_args.kwargs["json"]["prompt"] == _PROMPT
    assert span.attributes["llm.outcome"] == "http_error"
    assert span.attributes["llm.usage.prompt_eval_count"] == 12
    assert span.attributes["llm.usage.eval_count"] == 0
    assert span.attributes["llm.usage.total_duration"] == 0
    assert span.attributes["llm.usage.eval_duration"] == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [
        "ollama_failed: HTTP 503 model=llama3:latest detail=model runner overloaded"
    ]
    assert _PROMPT not in caplog.text
    events = _llm_events(caplog)
    assert len(events) == 1
    assert events[0]["outcome"] == "http_error"
    assert events[0]["prompt_chars"] == len(_PROMPT)
    assert events[0]["error"] == "model runner overloaded"
    assert events[0]["job_id"] == "job-7c2e"
    assert events[0]["llm_attempts"] == 1
    assert _PROMPT not in json.dumps(events[0])


def test_ollama_generate_exception_warning_omits_prompt(caplog):
    with caplog.at_level(logging.INFO), patch.object(
        lg, "settings", _worker_settings()
    ), patch.object(
        lg.httpx, "post", side_effect=ConnectionError("refused")
    ) as post:
        out = lg._ollama_generate(_PROMPT, job_id="job-7c2e")

    assert post.call_args.kwargs["json"]["prompt"] == _PROMPT
    parsed = lg._parse_json_obj(out)
    assert parsed["classification"] == "Unknown"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["ollama_failed: refused"]
    assert _PROMPT not in caplog.text
    events = _llm_events(caplog)
    assert events[0]["outcome"] == "exception"
    assert events[0]["prompt_chars"] == len(_PROMPT)
    assert events[0]["error"] == "refused"
    assert _PROMPT not in json.dumps(events[0])
