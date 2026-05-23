import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeResponse:
    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aread(self):
        return b"".join(self._chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("unexpected HTTP error in fake response")

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method, url):
        self.requests.append((method, url))
        return self.responses[url]


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 10 * 1024 * 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parse_rejects_file_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_parse_rejects_empty_url():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("   ")
    assert exc.value.status_code == 400


def test_parse_accepts_https():
    raw, host, port = _parse_and_validate_url("https://example.com/path?q=1")
    assert host == "example.com"
    assert "example.com" in raw
    assert port is None


def test_parse_rejects_invalid_port_as_client_error():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:not-a-port/doc")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid port"


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_classify_pdf_magic_overrides_octet_stream():
    assert _classify_body("application/octet-stream", b"%PDF-1.4\n1 0 obj") == "pdf"


def test_classify_html():
    assert _classify_body("text/html; charset=utf-8", b"<html></html>") == "html"


def test_classify_unknown_binary():
    assert _classify_body("application/zip", b"PK\x03\x04") == "unknown_binary"


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_fetch_revalidates_redirect_target_before_following():
    fake_client = _FakeAsyncClient(
        {
            "https://example.com/start": _FakeResponse(
                302,
                {"location": "http://169.254.169.254/latest/meta-data"},
            )
        }
    )
    checked_hosts = []

    def _check_host(host):
        checked_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    with patch("app.url_import.httpx.AsyncClient", return_value=fake_client), patch(
        "app.url_import.raise_for_private_or_meta_hosts", side_effect=_check_host
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert checked_hosts == ["example.com", "169.254.169.254"]
    assert fake_client.requests == [("GET", "https://example.com/start")]


def test_fetch_enforces_max_bytes_while_streaming():
    fake_client = _FakeAsyncClient(
        {
            "https://example.com/doc.txt": _FakeResponse(
                200,
                {"content-type": "text/plain"},
                [b"12345", b"67890", b"x"],
            )
        }
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=fake_client), patch(
        "app.url_import.raise_for_private_or_meta_hosts"
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/doc.txt",
                    _settings(url_import_max_bytes=10),
                )
            )

    assert exc.value.status_code == 413
    assert fake_client.requests == [("GET", "https://example.com/doc.txt")]
