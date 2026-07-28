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


def test_generate_llama3_empty_response_uses_fallback_string():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "   "}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200
    with patch("app.ollama_client.httpx.post", return_value=mock_resp), patch(
        "app.ollama_client.apply_ollama_response_to_span"
    ) as apply_span, patch("app.ollama_client.log_llm_event") as log_event:
        out = generate_llama3("p")

    assert out == "No answer returned."
    assert apply_span.call_args.kwargs["outcome"] == "empty_response"
    assert apply_span.call_args.kwargs["stage"] == "ingestion.rag"
    assert log_event.call_args.kwargs["outcome"] == "empty_response"


def test_generate_llama3_missing_response_key():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200
    with patch("app.ollama_client.httpx.post", return_value=mock_resp), patch(
        "app.ollama_client.apply_ollama_response_to_span"
    ) as apply_span, patch("app.ollama_client.log_llm_event") as log_event:
        out = generate_llama3("p")

    assert out == "No answer returned."
    assert apply_span.call_args.kwargs["outcome"] == "empty_response"
    assert log_event.call_args.kwargs["outcome"] == "empty_response"


def test_generate_llama3_non_json_body_is_empty_response():
    mock_resp = MagicMock()
    mock_resp.json.side_effect = ValueError("not json")
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200
    with patch("app.ollama_client.httpx.post", return_value=mock_resp), patch(
        "app.ollama_client.apply_ollama_response_to_span"
    ) as apply_span, patch("app.ollama_client.log_llm_event") as log_event:
        out = generate_llama3("p")

    assert out == "No answer returned."
    assert apply_span.call_args.kwargs["outcome"] == "empty_response"
    assert apply_span.call_args.kwargs["response_json"] is None
    assert log_event.call_args.kwargs["outcome"] == "empty_response"


def test_generate_llama3_success_records_ok_outcome():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "  answer text  \n"}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status_code = 200
    with patch("app.ollama_client.httpx.post", return_value=mock_resp), patch(
        "app.ollama_client.apply_ollama_response_to_span"
    ) as apply_span, patch("app.ollama_client.log_llm_event") as log_event:
        out = generate_llama3("my prompt")

    assert out == "answer text"
    assert apply_span.call_args.kwargs["outcome"] == "ok"
    assert apply_span.call_args.kwargs["stage"] == "ingestion.rag"
    assert log_event.call_args.kwargs["outcome"] == "ok"


def test_generate_llama3_on_exception_returns_unavailable_message():
    with patch(
        "app.ollama_client.httpx.post",
        side_effect=ConnectionError("refused"),
    ), patch("app.ollama_client.apply_ollama_response_to_span") as apply_span, patch(
        "app.ollama_client.log_llm_event"
    ) as log_event:
        out = generate_llama3("p")

    assert out == (
        "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
    )
    assert apply_span.call_args.kwargs["outcome"] == "exception"
    assert apply_span.call_args.kwargs["http_status_code"] is None
    assert log_event.call_args.kwargs["outcome"] == "exception"
    assert "refused" in str(log_event.call_args.kwargs["error"])
