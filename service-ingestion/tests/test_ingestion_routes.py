from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def test_internal_index_calls_index_document():
    from app.main import app

    with patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/internal/index",
            json={"job_id": "j1", "filename": "f.txt", "text": "hello"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "indexed"}
    idx.assert_called_once_with("j1", "f.txt", "hello")


def test_ask_returns_answer_question_payload():
    from app.main import app
    from app.ollama_client import generate_llama3

    payload = {"answer": "from stub", "snippets": [{"text": "x", "metadata": {}}]}
    with patch("app.main.answer_question", return_value=payload) as aq:
        client = TestClient(app)
        response = client.post("/ask", json={"question": "What?"})
    assert response.status_code == 200
    assert response.json() == payload
    aq.assert_called_once()
    assert aq.call_args.args[0] == "What?"
    assert aq.call_args.kwargs["llm_call"] is generate_llama3


def test_ask_rejects_blank_and_whitespace_questions():
    """Blank /ask must not embed or call the LLM (wasted cost / empty RAG noise)."""
    from app.main import app

    with patch("app.main.answer_question") as aq:
        client = TestClient(app)
        for question in ("", "   ", "\n\t  "):
            response = client.post("/ask", json={"question": question})
            assert response.status_code == 400, question
            assert response.json()["detail"] == "Question is required"
    aq.assert_not_called()


def test_ask_strips_surrounding_whitespace_before_answer_question():
    from app.main import app

    payload = {"answer": "ok", "snippets": []}
    with patch("app.main.answer_question", return_value=payload) as aq:
        client = TestClient(app)
        response = client.post("/ask", json={"question": "  What is X?  \n"})

    assert response.status_code == 200
    assert response.json() == payload
    aq.assert_called_once()
    assert aq.call_args.args[0] == "What is X?"


def test_cors_disallowed_origin_omits_allow_origin_header():
    """Only Vite dev origins are allowlisted; others must not receive ACAO."""
    from app.main import app

    client = TestClient(app)
    response = client.get(
        "/health",
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    headers_lower = {k.lower(): v for k, v in response.headers.items()}
    assert "access-control-allow-origin" not in headers_lower


def test_ask_endpoint_wires_ollama_and_retrieve_context():
    from app.main import app

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "synthesized"}
    mock_resp.raise_for_status = MagicMock()

    with patch("app.ollama_client.httpx.post", return_value=mock_resp) as post, patch(
        "app.rag_service.retrieve_context",
        return_value=(["ctx one"], [{"file": "a.txt"}], []),
    ):
        client = TestClient(app)
        response = client.post("/ask", json={"question": "Q1"})

    assert response.status_code == 200
    data = response.json()
    assert data["answer"] == "synthesized"
    assert data["snippets"] == [{"text": "ctx one", "metadata": {"file": "a.txt"}}]
    post.assert_called_once()
    prompt = post.call_args.kwargs["json"]["prompt"]
    assert "Q1" in prompt
    assert "ctx one" in prompt


def test_ask_endpoint_ollama_failure_returns_fallback_answer():
    from app.main import app

    with patch(
        "app.ollama_client.httpx.post",
        side_effect=ConnectionError("down"),
    ), patch(
        "app.rag_service.retrieve_context",
        return_value=(["ctx"], [{}], []),
    ):
        client = TestClient(app)
        response = client.post("/ask", json={"question": "Q?"})

    assert response.status_code == 200
    assert response.json()["answer"] == (
        "LLM unavailable; install Ollama and pull the model set in OLLAMA_MODEL."
    )


def test_documents_maps_dynamo_scan_items():
    from app.main import app

    mock_table = MagicMock()
    mock_table.scan.return_value = {
        "Items": [
            {
                "MessageId": "m1",
                "Status": "Completed",
                "Classification": "Tech",
                "Summary": "Done",
            },
        ],
    }
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    with patch("app.main.get_dynamodb_resource", return_value=mock_resource):
        client = TestClient(app)
        response = client.get("/documents")

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == [
        {
            "message_id": "m1",
            "status": "Completed",
            "classification": "Tech",
            "summary": "Done",
        },
    ]
    mock_resource.Table.assert_called_once()


def test_upload_s3_failure_returns_500():
    from app.main import app

    mock_s3 = MagicMock()
    mock_s3.upload_fileobj.side_effect = RuntimeError("s3 unavailable")

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ), patch("app.main.index_document"):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/upload",
            files={"file": ("t.txt", b"data", "text/plain")},
        )

    assert response.status_code == 500
