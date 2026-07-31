import asyncio
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile


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


@pytest.mark.parametrize("filename", [None, ""])
def test_upload_empty_filename_defaults_to_upload(filename):
    # Multipart rejects blank filenames before the handler; call upload directly.
    from app.main import upload

    mock_s3 = MagicMock()
    upload_file = UploadFile(file=BytesIO(b"hello nameless"), filename=filename)

    async def _run():
        with patch("app.main.get_s3_client", return_value=mock_s3), patch(
            "app.main.publish_job_safe"
        ) as pub, patch("app.main.index_document") as idx:
            data = await upload(file=upload_file)
            return data, pub, idx

    data, pub, idx = asyncio.run(_run())

    assert data["status"] == "uploaded"
    assert data["file"] == "upload"
    mock_s3.upload_fileobj.assert_called_once()
    assert mock_s3.upload_fileobj.call_args.args[2] == "upload"
    pub.assert_called_once()
    assert pub.call_args.kwargs["filename"] == "upload"
    assert pub.call_args.args[0].s3_key == "upload"
    idx.assert_called_once()
    assert idx.call_args.args[1] == "upload"


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
