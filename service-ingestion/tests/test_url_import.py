import asyncio
import socket
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
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


@pytest.mark.parametrize(
    "host",
    [
        "0x7f.0.1",  # three-part mixed hex loopback → 127.0.0.1
        "0177.0.1",  # three-part octal loopback → 127.0.0.1
        "10.0.1",  # three-part decimal RFC1918 → 10.0.0.1
    ],
)
def test_raise_for_private_blocks_three_part_encoded_hosts(host):
    """ipaddress rejects a.b.c forms; glibc getaddrinfo maps them to disallowed addresses."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_allows_three_part_public_literal():
    """ipaddress rejects 8.8.1; glibc maps it to public 8.8.0.1."""
    raise_for_private_or_meta_hosts("8.8.1")


def test_fetch_three_part_mixed_hex_loopback_is_blocked_before_get():
    """0x7f.0.1 is 127.0.0.1 via getaddrinfo; fetch must 403 before GET."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://0x7f.0.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_three_part_rfc1918_is_blocked_before_get():
    """10.0.1 is 10.0.0.1 via getaddrinfo; fetch must 403 before GET."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://10.0.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_three_part_octal_loopback_is_blocked_before_get():
    """0177.0.1 is 127.0.0.1 via getaddrinfo; fetch must 403 before GET."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://0177.0.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_three_part_public_literal_is_fetched():
    """8.8.1 is not private (three-part 8.8.0.1); fetch must proceed after DNS mapping."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"public-three-part",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        body, ext = asyncio.run(
            fetch_url_document("https://8.8.1/doc.txt", _url_import_settings())
        )
    assert body == b"public-three-part"
    assert ext == ".txt"
    assert requests == [("GET", "https://8.8.1/doc.txt")]


def test_fetch_xhtml_xml_extracts_plain_text_and_uses_txt_extension():
    """application/xhtml+xml is on the HTML allow-list; stored body is extracted .txt, not raw XML."""
    html = (
        b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
        b"<article><p>UniqueXhtmlImportMarker for stored document</p></article>"
        b"</body></html>"
    )
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "application/xhtml+xml; charset=utf-8"},
                chunks=(html,),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/page.xhtml", _url_import_settings())
        )
    assert ext == ".txt"
    assert b"UniqueXhtmlImportMarker" in body
    assert b"xmlns=" not in body
    assert requests == [("GET", "https://example.com/page.xhtml")]
