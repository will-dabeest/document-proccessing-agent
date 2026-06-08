import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.url_import as url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(self, status_code, headers=None, chunks=()):
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
            request = httpx.Request("GET", "https://example.test/")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "remote error", request=request, response=response
            )


class _FakeAsyncClient:
    def __init__(self, responses, requested_urls):
        self._responses = list(responses)
        self._requested_urls = requested_urls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        assert method == "GET"
        self._requested_urls.append(url)
        return self._responses.pop(0)


def _settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_public_dns(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (
                url_import.socket.AF_INET,
                url_import.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ]

    monkeypatch.setattr(url_import.socket, "getaddrinfo", fake_getaddrinfo)


def _patch_async_client(monkeypatch, responses):
    requested_urls = []

    def fake_client_factory(**_kwargs):
        return _FakeAsyncClient(responses, requested_urls)

    monkeypatch.setattr(url_import.httpx, "AsyncClient", fake_client_factory)
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


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_any_private_dns_result(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        assert host == "mixed.example"
        return [
            (
                url_import.socket.AF_INET,
                url_import.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            ),
            (
                url_import.socket.AF_INET,
                url_import.socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", 443),
            ),
        ]

    monkeypatch.setattr(url_import.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("mixed.example")

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


def test_fetch_url_revalidates_redirect_target_before_following(monkeypatch):
    _patch_public_dns(monkeypatch)
    requested_urls = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                302, headers={"location": "http://127.0.0.1/metadata"}
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document("https://example.test/start", _settings())
        )

    assert exc.value.status_code == 403
    assert requested_urls == ["https://example.test/start"]


def test_fetch_url_follows_relative_redirect_and_preserves_markdown_extension(
    monkeypatch,
):
    _patch_public_dns(monkeypatch)
    requested_urls = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(302, headers={"location": "/docs/readme"}),
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\n", b"Body"],
            ),
        ],
    )

    body, extension = asyncio.run(
        fetch_url_document("https://example.test/start", _settings())
    )

    assert body == b"# Title\n\nBody"
    assert extension == ".md"
    assert requested_urls == [
        "https://example.test/start",
        "https://example.test/docs/readme",
    ]


def test_fetch_url_rejects_response_over_configured_byte_limit(monkeypatch):
    _patch_public_dns(monkeypatch)
    requested_urls = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.test/large",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert requested_urls == ["https://example.test/large"]


def test_fetch_url_rejects_redirect_chain_past_configured_limit(monkeypatch):
    _patch_public_dns(monkeypatch)
    requested_urls = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(302, headers={"location": "/one"}),
            _FakeStreamResponse(302, headers={"location": "/two"}),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.test/start",
                _settings(url_import_max_redirects=1),
            )
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Too many redirects"
    assert requested_urls == [
        "https://example.test/start",
        "https://example.test/one",
    ]
