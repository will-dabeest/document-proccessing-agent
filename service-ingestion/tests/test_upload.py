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


def test_upload_same_basename_overwrites_shared_s3_key_with_distinct_job_ids():
    """Repeated uploads of the same filename share one S3 object key.

    Unlike /import-url (uuid-prefixed keys), /upload uses Path(...).name only.
    A second upload overwrites the object while minting a new job_id, so earlier
    workers can classify/index different bytes than the surviving object.
    """
    from app.main import app

    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe"
    ) as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        first = client.post(
            "/upload",
            files={"file": ("report.txt", b"first-body", "text/plain")},
        )
        second = client.post(
            "/upload",
            files={"file": ("report.txt", b"second-body", "text/plain")},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["file"] == "report.txt"
    assert second.json()["file"] == "report.txt"
    assert first.json()["job_id"] != second.json()["job_id"]

    assert mock_s3.upload_fileobj.call_count == 2
    keys = [c.args[2] for c in mock_s3.upload_fileobj.call_args_list]
    assert keys == ["report.txt", "report.txt"]

    uploaded_bodies = []
    for call in mock_s3.upload_fileobj.call_args_list:
        buf = call.args[0]
        buf.seek(0)
        uploaded_bodies.append(buf.read())
    assert uploaded_bodies == [b"first-body", b"second-body"]

    assert pub.call_count == 2
    assert pub.call_args_list[0].args[0].s3_key == "report.txt"
    assert pub.call_args_list[1].args[0].s3_key == "report.txt"
    assert (
        pub.call_args_list[0].args[0].idempotency_key
        != pub.call_args_list[1].args[0].idempotency_key
    )

    assert idx.call_count == 2
    assert idx.call_args_list[0].args[:3] == (
        first.json()["job_id"],
        "report.txt",
        "first-body",
    )
    assert idx.call_args_list[1].args[:3] == (
        second.json()["job_id"],
        "report.txt",
        "second-body",
    )


def test_health():
    from app.main import app

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
