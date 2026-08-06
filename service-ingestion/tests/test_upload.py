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


def test_safe_upload_basename_strips_posix_and_windows_paths():
    from app.main import _safe_upload_basename

    assert _safe_upload_basename("../../secret.txt") == "secret.txt"
    assert _safe_upload_basename("..\\..\\secret.txt") == "secret.txt"
    assert _safe_upload_basename("folder\\nested\\doc.md") == "doc.md"
    assert _safe_upload_basename("..") == "upload"
    assert _safe_upload_basename(".") == "upload"
    assert _safe_upload_basename("") == "upload"
    assert _safe_upload_basename(None) == "upload"


def test_upload_sanitizes_backslash_traversal_before_storage_and_indexing():
    """Path.name alone does not strip '\\' on Linux; backslash paths must not leak."""
    from app.main import app, settings

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={
                "file": ("..\\..\\secret.txt", b"sensitive text", "text/plain"),
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["file"] == "secret.txt"

    mock_s3.upload_fileobj.assert_called_once()
    _buf, bucket, key = mock_s3.upload_fileobj.call_args.args
    assert bucket == settings.bucket_name
    assert key == "secret.txt"
    pub.assert_called_once()
    assert pub.call_args.args[0].s3_key == "secret.txt"
    assert pub.call_args.kwargs == {"filename": "secret.txt"}
    idx.assert_called_once_with(data["job_id"], "secret.txt", "sensitive text")


def test_upload_dotdot_basename_falls_back_to_upload_key():
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("..", b"payload", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["file"] == "upload"
    assert mock_s3.upload_fileobj.call_args.args[2] == "upload"
    assert pub.call_args.args[0].s3_key == "upload"
    idx.assert_called_once()
    assert idx.call_args.args[1] == "upload"


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
