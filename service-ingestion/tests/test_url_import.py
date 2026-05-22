import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    fetch_url_document,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 2,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [b"hello"]
        self.request = httpx.Request("GET", "https://example.com/doc")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError(
                "status error", request=self.request, response=response
            )

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        if not self.responses:
            raise AssertionError(f"Unexpected request to {url}")
        return self.responses.pop(0)


def _run_fetch(url, client, settings=None):
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ],
    ):
        return asyncio.run(fetch_url_document(url, settings or _settings()))


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


def test_fetch_revalidates_redirect_target_before_second_request():
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ]
    )

    with pytest.raises(UrlImportError) as exc:
        _run_fetch("https://example.com/start", client)

    assert exc.value.status_code == 403
    assert client.requests == [("GET", "https://example.com/start")]


def test_fetch_enforces_streamed_response_size_limit():
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            )
        ]
    )

    with pytest.raises(UrlImportError) as exc:
        _run_fetch(
            "https://example.com/large.txt",
            client,
            settings=_settings(url_import_max_bytes=5),
        )

    assert exc.value.status_code == 413


def test_fetch_maps_remote_http_status_to_bad_gateway():
    client = _FakeAsyncClient([_FakeStreamResponse(status_code=404)])

    with pytest.raises(UrlImportError) as exc:
        _run_fetch("https://example.com/missing", client)

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_preserves_markdown_extension_for_markdown_content_type():
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n"],
            )
        ]
    )

    body, ext = _run_fetch("https://example.com/readme.md", client)

    assert body == b"# Title\n"
    assert ext == ".md"
