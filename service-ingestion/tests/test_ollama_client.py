from unittest.mock import MagicMock, patch

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


def test_generate_llama3_posts_to_configured_generate_url():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "ok"}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200
    fake_settings = MagicMock()
    fake_settings.ollama_base_url = "http://llm:11434"
    fake_settings.ollama_model = "custom-model"
    fake_settings.ollama_http_timeout_seconds = 9.0

    with patch("app.ollama_client.httpx.post", return_value=mock_resp) as post, patch(
        "app.ollama_client.settings", fake_settings
    ):
        out = generate_llama3("prompt-x")

    assert out == "ok"
    post.assert_called_once_with(
        "http://llm:11434/api/generate",
        json={"model": "custom-model", "prompt": "prompt-x", "stream": False},
        timeout=9.0,
    )


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
