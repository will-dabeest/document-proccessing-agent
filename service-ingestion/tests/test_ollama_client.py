from unittest.mock import MagicMock, patch

import httpx

from app.ollama_client import generate_llama3


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


def test_generate_llama3_http_status_error_returns_unavailable_message():
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    error_response = httpx.Response(503, request=request)
    mock_resp = MagicMock(status_code=503)
    mock_resp.json.return_value = {"error": "model busy"}
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "model busy",
        request=request,
        response=error_response,
    )

    with patch("app.ollama_client.httpx.post", return_value=mock_resp) as post, patch(
        "app.ollama_client.log_llm_event"
    ) as log_event, patch(
        "app.ollama_client.settings.ollama_http_timeout_seconds", 42.5
    ):
        out = generate_llama3("rag prompt")

    assert out == (
        "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
    )
    assert post.call_args.kwargs["timeout"] == 42.5
    assert log_event.call_args.kwargs["outcome"] == "http_error"
    assert log_event.call_args.kwargs["http_status_code"] == 503
    assert log_event.call_args.kwargs["error"] == "model busy"
    assert log_event.call_args.kwargs["stage"] == "ingestion.rag"
