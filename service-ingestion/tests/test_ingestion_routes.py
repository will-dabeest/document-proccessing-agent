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

    payload = {"answer": "from stub", "snippets": [{"text": "x", "metadata": {}}]}
    with patch("app.main.answer_question", return_value=payload) as aq:
        client = TestClient(app)
        response = client.post("/ask", json={"question": "What?"})
    assert response.status_code == 200
    assert response.json() == payload
    aq.assert_called_once()
    assert aq.call_args.kwargs["llm_call"] is not None


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
