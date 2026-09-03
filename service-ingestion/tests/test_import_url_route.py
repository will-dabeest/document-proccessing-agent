from unittest.mock import MagicMock, patch

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


def test_import_url_ignores_extra_json_fields_for_storage_key_and_job_id(mock_s3):
    """ImportUrlRequest only binds `url`. Caller-supplied s3_key/job_id must not select the object key."""
    from app.main import app

    with patch("app.main.get_s3_client", return_value=mock_s3), patch(
        "app.main.fetch_url_document", new=_fetch_text
    ), patch("app.main.publish_job_safe") as pub, patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/import-url",
            json={
                "url": "https://example.com/doc",
                "s3_key": "../passwd",
                "job_id": "attacker-chosen",
                "file": "owned.txt",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "imported"
    assert data["file"].startswith("imports/")
    assert data["file"].endswith(".txt")
    assert "../passwd" not in data["file"]
    assert "owned" not in data["file"]
    assert data["job_id"] != "attacker-chosen"
    pub.assert_called_once()
    idx.assert_called_once()
    uploaded_key = mock_s3.upload_fileobj.call_args.args[2]
    assert uploaded_key == data["file"]


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
