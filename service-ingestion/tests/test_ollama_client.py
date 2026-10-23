import json
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ollama_client import generate_llama3

_PROMPT = "PROMPT_MARKER_7c2e_private_clause"
_USAGE_ZEROS = {
    "prompt_eval_count": 0,
    "eval_count": 0,
    "total_duration": 0,
    "load_duration": 0,
    "prompt_eval_duration": 0,
    "eval_duration": 0,
}


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def __enter__(self) -> "_RecordingSpan":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("server error", request=request, response=response)


def _llm_events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    events = []
    for rec in caplog.records:
        message = rec.getMessage()
        if message.startswith("llm_event "):
            events.append(json.loads(message.removeprefix("llm_event ")))
    return events


def test_generate_llama3_success_returns_stripped_response():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "  answer text  \n"}
    mock_resp.raise_for_status = MagicMock()
    with patch("app.ollama_client.httpx.post", return_value=mock_resp) as post:
        out = generate_llama3("my prompt")

    assert out == "answer text"
    post.assert_called_once()
    call_kw = post.call_args.kwargs
    assert call_kw["json"]["model"] == "llama3:latest"
    assert call_kw["json"]["prompt"] == "my prompt"
    assert call_kw["json"]["stream"] is False


def test_generate_llama3_empty_response_uses_fallback_string():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "   "}
    mock_resp.raise_for_status = MagicMock()
    with patch("app.ollama_client.httpx.post", return_value=mock_resp):
        out = generate_llama3("p")

    assert out == "No answer returned."


def test_generate_llama3_missing_response_key():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_resp.raise_for_status = MagicMock()
    with patch("app.ollama_client.httpx.post", return_value=mock_resp):
        out = generate_llama3("p")

    assert out == "No answer returned."


def test_generate_llama3_on_exception_returns_unavailable_message():
    with patch(
        "app.ollama_client.httpx.post",
        side_effect=ConnectionError("refused"),
    ):
        out = generate_llama3("p")

    assert out == (
        "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
    )


def test_generate_llama3_records_exact_zero_usage_on_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"response": "answer text", **_USAGE_ZEROS}
    mock_resp.raise_for_status = MagicMock()
    span = _RecordingSpan()
    with patch("app.ollama_client.httpx.post", return_value=mock_resp), patch(
        "app.ollama_client.tracer.start_as_current_span", return_value=span
    ):
        out = generate_llama3(_PROMPT)

    assert out == "answer text"
    assert span.attributes["llm.outcome"] == "ok"
    for key, value in _USAGE_ZEROS.items():
        assert span.attributes[f"llm.usage.{key}"] == value
    assert all(_PROMPT not in str(value) for value in span.attributes.values())


def test_generate_llama3_http_error_keeps_zero_usage_and_omits_prompt(caplog):
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
    with caplog.at_level(logging.INFO), patch(
        "app.ollama_client.httpx.post", return_value=mock_resp
    ) as post, patch(
        "app.ollama_client.tracer.start_as_current_span", return_value=span
    ):
        out = generate_llama3(_PROMPT)

    assert out == (
        "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
    )
    assert post.call_args.kwargs["json"]["prompt"] == _PROMPT
    assert span.attributes["llm.outcome"] == "http_error"
    assert span.attributes["http.status_code"] == 503
    assert span.attributes["llm.usage.prompt_eval_count"] == 12
    assert span.attributes["llm.usage.eval_count"] == 0
    assert span.attributes["llm.usage.eval_duration"] == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [
        "ollama_generate_failed: HTTP 503 model=llama3:latest detail=model runner overloaded"
    ]
    assert _PROMPT not in caplog.text
    events = _llm_events(caplog)
    assert len(events) == 1
    assert events[0]["outcome"] == "http_error"
    assert events[0]["prompt_chars"] == len(_PROMPT)
    assert events[0]["error"] == "model runner overloaded"
    assert _PROMPT not in json.dumps(events[0])


def test_generate_llama3_exception_warning_omits_prompt(caplog):
    with caplog.at_level(logging.INFO), patch(
        "app.ollama_client.httpx.post",
        side_effect=ConnectionError("refused"),
    ) as post:
        out = generate_llama3(_PROMPT)

    assert out == (
        "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
    )
    assert post.call_args.kwargs["json"]["prompt"] == _PROMPT
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["ollama_generate_failed: refused"]
    assert _PROMPT not in caplog.text
    events = _llm_events(caplog)
    assert events[0]["outcome"] == "exception"
    assert events[0]["prompt_chars"] == len(_PROMPT)
    assert events[0]["error"] == "refused"
    assert _PROMPT not in json.dumps(events[0])
