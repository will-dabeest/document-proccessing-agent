import asyncio
from types import SimpleNamespace

import pytest

import app.url_import as url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected fake HTTP status {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, responses, requests):
        self._responses = list(responses)
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        if not self._responses:
            raise AssertionError(f"unexpected request to {url}")
        return self._responses.pop(0)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1,
        "url_import_max_redirects": 3,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_async_client(monkeypatch, responses):
    requests = []

    def fake_async_client(*_args, **_kwargs):
        return _FakeAsyncClient(responses, requests)

    monkeypatch.setattr(url_import.httpx, "AsyncClient", fake_async_client)
    return requests


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


def test_fetch_url_document_revalidates_redirect_targets(monkeypatch):
    requests = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            ),
        ],
    )
    validated_hosts = []

    def validate_host(host):
        validated_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", validate_host)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/start",
                _settings(),
            )
        )

    assert exc.value.status_code == 403
    assert validated_hosts == ["example.com", "169.254.169.254"]
    assert requests == [("GET", "https://example.com/start")]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    requests = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                status_code=200,
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            ),
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert requests == [("GET", "https://example.com/large.txt")]
