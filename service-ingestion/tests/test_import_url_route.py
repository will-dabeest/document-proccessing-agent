from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_s3():
    return MagicMock()


async def _fetch_text(*_a, **_kw):
    return (b"hello from url", ".txt")


async def _fetch_pdf(*_a, **_kw):
    return (b"%PDF-1.4 fake binary \xff", ".pdf")


def test_import_url_success(mock_s3):
    from app.main import app

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.fetch_url_document", new=_fetch_text
    ), patch("app.main.publish_job_safe") as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post("/import-url", json={"url": "https://example.com/doc"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "imported"
    assert data["file"].startswith("imports/")
    assert data["file"].endswith(".txt")
    assert "job_id" in data
    pub.assert_called_once()
    idx.assert_called_once()
    mock_s3.upload_fileobj.assert_called_once()


def test_import_url_storage_key_ignores_url_path_and_is_unique(mock_s3):
    """S3 keys are imports/<uuid>.ext so URL path / Content-Disposition cannot collide or traverse."""
    from app.main import app

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.fetch_url_document", new=_fetch_text
    ), patch("app.main.publish_job_safe") as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        first = client.post(
            "/import-url",
            json={"url": "https://evil.example/../../etc/passwd.exe?x=1"},
        )
        second = client.post(
            "/import-url",
            json={"url": "https://evil.example/../../etc/passwd.exe?x=1"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    key1 = first.json()["file"]
    key2 = second.json()["file"]
    assert key1 != key2
    for key, job_id in (
        (key1, first.json()["job_id"]),
        (key2, second.json()["job_id"]),
    ):
        assert key.startswith("imports/")
        assert key.endswith(".txt")
        UUID(key[len("imports/") : -len(".txt")])
        assert "passwd" not in key
        assert "etc" not in key
        assert ".." not in key
        assert job_id not in key

    assert mock_s3.upload_fileobj.call_count == 2
    stored_keys = [c.args[2] for c in mock_s3.upload_fileobj.call_args_list]
    assert stored_keys == [key1, key2]

    assert pub.call_count == 2
    assert pub.call_args_list[0].args[0].s3_key == key1
    assert pub.call_args_list[0].kwargs == {"filename": key1}
    assert pub.call_args_list[1].args[0].s3_key == key2
    assert pub.call_args_list[1].kwargs == {"filename": key2}

    assert idx.call_count == 2
    idx.assert_any_call(first.json()["job_id"], key1, "hello from url")
    idx.assert_any_call(second.json()["job_id"], key2, "hello from url")


def test_import_url_pdf_skips_sync_index(mock_s3):
    from app.main import app

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.fetch_url_document", new=_fetch_pdf
    ), patch("app.main.publish_job_safe"), patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post("/import-url", json={"url": "https://example.com/a.pdf"})

    assert response.status_code == 200
    assert response.json()["file"].endswith(".pdf")
    idx.assert_not_called()


def test_import_url_publish_failure_returns_502(mock_s3):
    from app.main import app

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.fetch_url_document", new=_fetch_text
    ), patch("app.main.publish_job_safe", side_effect=RuntimeError("sqs down")), patch(
        "app.main.index_document"
    ):
        client = TestClient(app)
        response = client.post("/import-url", json={"url": "https://example.com/x"})

    assert response.status_code == 502


def test_import_url_propagates_url_import_error(mock_s3):
    from app.main import app
    from app.url_import import UrlImportError

    async def _raise(*_a, **_kw):
        raise UrlImportError(415, "Unsupported content type")

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.fetch_url_document", new=_raise
    ):
        client = TestClient(app)
        response = client.post("/import-url", json={"url": "https://example.com/x"})

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported content type"


def test_import_url_disabled_returns_403(mock_s3):
    from app import config
    from app.main import app

    with patch.object(config.settings, "url_import_enabled", False), patch(
        "app.main.get_s3_client", return_value=mock_s3
    ), patch("app.main.publish_job_safe"), patch("app.main.index_document"):
        client = TestClient(app)
        response = client.post("/import-url", json={"url": "https://example.com/x"})

    assert response.status_code == 403
