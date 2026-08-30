import asyncio
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi import UploadFile
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


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_upload_none_filename_stores_as_upload():
    """Starlette may pass filename=None; the handler must still pick a stable S3 key."""
    from app.main import upload

    mock_s3 = MagicMock()
    uf = UploadFile(file=BytesIO(b"hello"), filename=None)
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document") as idx:
        result = asyncio.run(upload(uf))

    assert result["status"] == "uploaded"
    assert result["file"] == "upload"
    assert mock_s3.upload_fileobj.call_args.args[2] == "upload"
    pub.assert_called_once()
    idx.assert_called_once()
    assert idx.call_args.args[1] == "upload"
    assert idx.call_args.args[2] == "hello"
