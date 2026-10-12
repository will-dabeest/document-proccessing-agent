"""Upload stores the object before publish. A publish failure must say so and must not index."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def test_upload_publish_failure_keeps_stored_object_and_skips_index(monkeypatch):
    from app.main import app, settings

    monkeypatch.setattr(settings, "bucket_name", "bucket-under-test")
    mock_s3 = MagicMock()
    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.publish_job_safe", side_effect=RuntimeError("sqs down")
    ) as publish, patch("app.main.index_document") as index_document:
        client = TestClient(app)
        response = client.post(
            "/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Upload stored but job publish failed"
    mock_s3.upload_fileobj.assert_called_once()
    _body, bucket, key = mock_s3.upload_fileobj.call_args.args
    assert bucket == "bucket-under-test"
    assert key == "notes.txt"
    publish.assert_called_once()
    index_document.assert_not_called()
