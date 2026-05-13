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


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
