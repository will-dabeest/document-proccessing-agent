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


class FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.request_url = "https://example.com/"

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
        if self.status_code < 400:
            return
        request = httpx.Request("GET", self.request_url)
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f"status {self.status_code}",
            request=request,
            response=response,
        )


class FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        assert method == "GET"
        self.requests.append(url)
        if not self._responses:
            raise AssertionError(f"Unexpected request to {url}")
        response = self._responses.pop(0)
        response.request_url = url
        return response


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _allow_public_dns(monkeypatch):
    monkeypatch.setattr(
        url_import.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, "", ("93.184.216.34", 443)),
        ],
    )


def _install_fake_client(monkeypatch, responses):
    fake_client = FakeAsyncClient(responses)
    monkeypatch.setattr(
        url_import.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: fake_client,
    )
    return fake_client


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


def test_parse_rejects_invalid_port():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:notaport/path")

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


def test_fetch_revalidates_redirect_target_before_follow_up(monkeypatch):
    _allow_public_dns(monkeypatch)
    fake_client = _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(
                status_code=302,
                headers={"location": "http://127.0.0.1/internal"},
            ),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert fake_client.requests == ["https://example.com/start"]


def test_fetch_enforces_streamed_response_byte_limit(monkeypatch):
    _allow_public_dns(monkeypatch)
    _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            ),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert exc.value.detail == "Response body exceeds configured limit"


def test_fetch_maps_remote_http_status_to_bad_gateway(monkeypatch):
    _allow_public_dns(monkeypatch)
    _install_fake_client(monkeypatch, [FakeStreamResponse(status_code=404)])

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_preserves_markdown_extension_and_body(monkeypatch):
    _allow_public_dns(monkeypatch)
    _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n", b"Body text\n"],
            ),
        ],
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/readme.md", _settings())
    )

    assert ext == ".md"
    assert body == b"# Title\nBody text\n"
