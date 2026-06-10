import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.url_import as url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(
        self,
        status_code,
        *,
        headers=None,
        chunks=None,
        url="https://example.com/doc",
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._url = url

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
            request = httpx.Request("GET", self._url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        if not self._responses:
            raise AssertionError(f"Unexpected request to {url}")
        return self._responses.pop(0)


def _url_settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_max_bytes": 10,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


def test_fetch_url_revalidates_redirect_target_before_followup_request(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ]
    )
    checked_hosts = []

    def reject_metadata(host):
        checked_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", reject_metadata)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document("https://example.com/start", _url_settings())
        )

    assert exc.value.status_code == 403
    assert checked_hosts == ["example.com", "169.254.169.254"]
    assert fake_client.requests == [("GET", "https://example.com/start")]


def test_fetch_url_enforces_streamed_body_limit(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"1234", b"56"],
            )
        ]
    )
    monkeypatch.setattr(url_import.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/large", _url_settings(url_import_max_bytes=5)
            )
        )

    assert exc.value.status_code == 413
    assert fake_client.requests == [("GET", "https://example.com/large")]


def test_fetch_url_maps_remote_http_status_to_bad_gateway(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                404,
                headers={"content-type": "text/plain"},
                url="https://example.com/missing",
            )
        ]
    )
    monkeypatch.setattr(url_import.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/missing", _url_settings()
            )
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"
