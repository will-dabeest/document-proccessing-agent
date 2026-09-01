import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _html_to_plain_text,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


def _url_import_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


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

    def raise_for_status(self):
        # Mirrors httpx: only 4xx/5xx raise. 2xx and 3xx succeed.
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses, requests):
        self._responses = list(responses)
        self._requests = requests

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self._requests.append((method, url))
        return self._responses.pop(0)


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
    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_html_whitespace_only_first_extract_retries_favor_recall():
    """Strip of a whitespace-only first pass must still trigger the recall retry."""
    calls: list[bool] = []

    def fake_extract(_decoded, output_format="txt", favor_recall=False, **_kwargs):
        calls.append(favor_recall)
        if not favor_recall:
            return "  \n\t  "
        return "Recovered body text"

    with patch("app.url_import.trafilatura.extract", side_effect=fake_extract):
        out = _html_to_plain_text(b"<html><body>ignored</body></html>")

    assert out == b"Recovered body text"
    assert calls == [False, True]


@pytest.mark.parametrize("status_code", [201, 202, 206])
def test_fetch_non_200_success_statuses_are_stored(status_code):
    """httpx only raises for 4xx/5xx, so 201/202/206 bodies are stored as complete docs."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=status_code,
                headers={"content-type": "text/plain", "content-range": "bytes 0-4/99"},
                chunks=(b"Hello",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/doc.txt", _url_import_settings())
        )

    assert body == b"Hello"
    assert ext == ".txt"
    assert requests == [("GET", "https://example.com/doc.txt")]


def test_fetch_no_content_204_is_unsupported():
    """204 is a successful status with an empty body; classification still rejects it."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [_FakeStreamResponse(status_code=204, headers={}, chunks=())],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/empty", _url_import_settings())
            )

    assert exc.value.status_code == 415
    assert "Unsupported content type" in exc.value.detail
    assert requests == [("GET", "https://example.com/empty")]
