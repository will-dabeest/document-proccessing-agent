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


def test_classify_node_json_repair_passes_repair_stage_and_job_id():
    """JSON repair must call Ollama with the dedicated stage for telemetry."""
    calls: list[dict] = []

    def fake_generate(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        if len(calls) == 1:
            return "not valid json at all"
        return '{"classification": "Fixed", "summary": "Repaired."}'

    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node(
            {"document_text": "hello", "attempts": 0, "job_id": "jid-repair"}
        )

    assert out["classification"] == "Fixed"
    assert out["summary"] == "Repaired."
    assert len(calls) == 2
    assert calls[0]["stage"] == "worker.classify"
    assert calls[0]["job_id"] == "jid-repair"
    assert calls[0]["attempts"] == 0
    assert calls[1]["stage"] == "worker.classify_json_repair"
    assert calls[1]["job_id"] == "jid-repair"
    assert calls[1]["attempts"] == 0
    assert "invalid JSON" in calls[1]["prompt"]


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


def test_ollama_generate_empty_response_returns_empty_string_and_logs_outcome():
    """A 200 with blank model text must not use the HTTP-error fallback JSON."""
    fake_settings = SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://ollama.test:11434",
        ollama_model="llama3:test",
        ollama_http_timeout_seconds=45.0,
    )
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"response": "   "}
    mock_response.raise_for_status = MagicMock()

    with patch.object(lg, "settings", fake_settings), patch.object(
        lg.httpx, "post", return_value=mock_response
    ) as post, patch.object(lg, "log_llm_event") as log_event:
        out = lg._ollama_generate(
            "prompt", stage="worker.classify", job_id="job-empty"
        )

    assert out == ""
    assert post.call_args.kwargs["timeout"] == 45.0
    assert log_event.call_args.kwargs["outcome"] == "empty_response"
    assert log_event.call_args.kwargs["http_status_code"] == 200
    assert log_event.call_args.kwargs["job_id"] == "job-empty"
    assert log_event.call_args.kwargs["llm_stage"] == "worker.classify"


def test_ollama_generate_non_json_body_is_empty_response():
    fake_settings = SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://ollama.test:11434",
        ollama_model="llama3:test",
        ollama_http_timeout_seconds=30.0,
    )
    mock_response = MagicMock(status_code=200)
    mock_response.json.side_effect = ValueError("not json")
    mock_response.raise_for_status = MagicMock()

    with patch.object(lg, "settings", fake_settings), patch.object(
        lg.httpx, "post", return_value=mock_response
    ), patch.object(lg, "log_llm_event") as log_event:
        out = lg._ollama_generate("prompt")

    assert out == ""
    assert log_event.call_args.kwargs["outcome"] == "empty_response"


def test_classify_node_empty_ollama_response_triggers_json_repair():
    """Blank Ollama output is not fallback JSON, so classify must enter repair."""
    fake_settings = SimpleNamespace(
        llm_mock_json=None,
        ollama_base_url="http://ollama.test:11434",
        ollama_model="llama3:test",
        ollama_http_timeout_seconds=30.0,
    )
    empty_resp = MagicMock(status_code=200)
    empty_resp.json.return_value = {"response": ""}
    empty_resp.raise_for_status = MagicMock()
    repair_resp = MagicMock(status_code=200)
    repair_resp.json.return_value = {
        "response": '{"classification": "Ops", "summary": "Recovered."}'
    }
    repair_resp.raise_for_status = MagicMock()

    with patch.object(lg, "settings", fake_settings), patch.object(
        lg.httpx, "post", side_effect=[empty_resp, repair_resp]
    ), patch.object(lg, "log_llm_event") as log_event:
        out = lg.classify_node({"document_text": "text", "job_id": "j-empty"})

    assert out["classification"] == "Ops"
    assert out["summary"] == "Recovered."
    assert out["attempts"] == 1
    outcomes = [c.kwargs["outcome"] for c in log_event.call_args_list]
    stages = [c.kwargs.get("llm_stage") for c in log_event.call_args_list]
    assert "empty_response" in outcomes
    assert "worker.classify" in stages
    assert "worker.classify_json_repair" in stages


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
