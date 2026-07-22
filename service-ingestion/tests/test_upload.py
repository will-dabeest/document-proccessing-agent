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


def test_upload_sanitizes_filename_before_storage_and_indexing():
    from app.main import app, settings

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("../../secret.txt", b"sensitive text", "text/plain")},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["file"] == "secret.txt"

    mock_s3.upload_fileobj.assert_called_once()
    upload_buffer, bucket, key = mock_s3.upload_fileobj.call_args.args
    assert upload_buffer.getvalue() == b"sensitive text"
    assert bucket == settings.bucket_name
    assert key == "secret.txt"

    pub.assert_called_once()
    assert pub.call_args.args[0].s3_key == "secret.txt"
    assert pub.call_args.kwargs == {"filename": "secret.txt"}
    idx.assert_called_once_with(data["job_id"], "secret.txt", "sensitive text")


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
