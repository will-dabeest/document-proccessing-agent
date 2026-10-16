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


def test_upload_indexes_utf8_body_with_created_job_id():
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ), patch("app.main.index_document") as index_document:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("notes.txt", b"hello indexed world", "text/plain")},
        )

    assert response.status_code == 200
    data = response.json()
    index_document.assert_called_once_with(
        data["job_id"],
        "notes.txt",
        "hello indexed world",
    )


def test_upload_skips_indexing_non_utf8_body():
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ), patch("app.main.index_document") as index_document:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("scan.pdf", b"\xff\xfe\xfd", "application/pdf")},
        )

    assert response.status_code == 200
    index_document.assert_not_called()


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
