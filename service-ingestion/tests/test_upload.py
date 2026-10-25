from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def test_upload_returns_200():
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document"):
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "uploaded"
        assert data["file"] == "test.txt"
        assert "job_id" in data
        pub.assert_called_once()


def test_upload_publish_failure_returns_502():
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe", side_effect=RuntimeError("sqs down")
    ), patch("app.main.index_document"):
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 502


def test_upload_index_failure_is_not_reported_as_uploaded():
    from app.main import app

    marker = "DOC_MARKER_67fd_upload_clause"
    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch(
        "app.main.index_document", side_effect=RuntimeError("chroma down")
    ) as idx:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/upload",
            files={"file": ("notes.txt", marker.encode("utf-8"), "text/plain")},
        )

    assert response.status_code == 500
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        assert payload.get("status") != "uploaded"
    assert marker not in response.text
    pub.assert_called_once()
    job = pub.call_args.args[0]
    assert pub.call_args.kwargs["filename"] == "notes.txt"
    idx.assert_called_once_with(job.idempotency_key, "notes.txt", marker)
    assert mock_s3.upload_fileobj.call_args.args[2] == "notes.txt"


def test_upload_invalid_utf8_skips_sync_index_and_returns_uploaded():
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("scan.pdf", b"%PDF-1.4\xff not utf-8", "application/pdf")},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "uploaded"
    assert data["file"] == "scan.pdf"
    pub.assert_called_once()
    assert pub.call_args.kwargs["filename"] == "scan.pdf"
    assert pub.call_args.args[0].s3_key == "scan.pdf"
    idx.assert_not_called()
    assert mock_s3.upload_fileobj.call_args.args[2] == "scan.pdf"


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
