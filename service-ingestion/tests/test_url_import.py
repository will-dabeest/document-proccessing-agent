import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.url = "https://example.com/"

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code < 400:
            return
        request = httpx.Request("GET", self.url)
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("remote error", request=request, response=response)


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _install_fake_async_client(monkeypatch, routes):
    requested_urls = []

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            requested_urls.append(url)
            response = routes[url]
            response.url = url
            return _FakeStream(response)

    monkeypatch.setattr(url_import.httpx, "AsyncClient", FakeAsyncClient)
    return requested_urls


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


def test_parse_rejects_invalid_port_with_import_error():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:not-a-port/path")
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


def test_fetch_revalidates_redirect_target_before_following(monkeypatch):
    requested_urls = _install_fake_async_client(
        monkeypatch,
        {
            "https://example.com/start": _FakeResponse(
                302,
                headers={
                    "location": "http://169.254.169.254/latest/meta-data/iam/"
                },
            )
        },
    )
    validated_hosts = []

    def reject_metadata_host(host):
        validated_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(
        url_import, "raise_for_private_or_meta_hosts", reject_metadata_host
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert validated_hosts == ["example.com", "169.254.169.254"]
    assert requested_urls == ["https://example.com/start"]


def test_fetch_enforces_streamed_size_limit(monkeypatch):
    requested_urls = _install_fake_async_client(
        monkeypatch,
        {
            "https://example.com/large.txt": _FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"123", b"456"],
            )
        },
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _h: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert requested_urls == ["https://example.com/large.txt"]


def test_fetch_maps_remote_http_status_to_import_error(monkeypatch):
    _install_fake_async_client(
        monkeypatch,
        {"https://example.com/missing": _FakeResponse(404)},
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _h: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_preserves_markdown_extension(monkeypatch):
    _install_fake_async_client(
        monkeypatch,
        {
            "https://example.com/readme.md": _FakeResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nBody"],
            )
        },
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _h: None)

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/readme.md", _settings())
    )

    assert body == b"# Title\n\nBody"
    assert ext == ".md"
