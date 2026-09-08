import asyncio
import socket
import zlib
from types import SimpleNamespace
from unittest.mock import patch

import httpx
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
        "0x7f.1",  # mixed hex + short-form loopback → 127.0.0.1
        "10.0.0.0xa",  # last-octet hex 10.0.0.10
        "0xc0.168.0.1",  # mixed hex/decimal 192.168.0.1
        "192.168",  # two-octet form → 192.0.0.168 (IANA special-purpose)
    ],
)
def test_raise_for_private_blocks_mixed_hex_short_and_two_octet_encodings(host):
    """ipaddress rejects these; glibc getaddrinfo maps them to disallowed addresses."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.1",  # this-network (0.0.0.0/8), distinct from unspecified 0.0.0.0
        "fd12::1",  # ULA distinct from fc00::1
        "2001::1",  # Teredo 2001::/32
    ],
)
def test_raise_for_private_blocks_this_network_ula_and_teredo_literals(host):
    """These parse as IP literals; SSRF checks must not skip DNS and then allow them."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_allows_decimal_public_literal():
    """ipaddress rejects 134744074; glibc maps it to public 8.8.8.10."""
    raise_for_private_or_meta_hosts("134744074")


def test_fetch_mixed_hex_loopback_is_blocked_before_get():
    """0x7f.1 is 127.0.0.1 via getaddrinfo; fetch must 403 before GET."""
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
                fetch_url_document("https://0x7f.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_last_octet_hex_rfc1918_is_blocked_before_get():
    """10.0.0.0xa is 10.0.0.10 via getaddrinfo; fetch must 403 before GET."""
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
                fetch_url_document("https://10.0.0.0xa/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_ula_literal_is_blocked_without_dns():
    """fd12::1 is unique-local; must 403 as a literal (no DNS, no GET)."""
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
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=AssertionError("dns")
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://[fd12::1]/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_teredo_literal_is_blocked_without_dns():
    """2001::1 is Teredo; must 403 as a literal (no DNS, no GET)."""
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
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=AssertionError("dns")
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://[2001::1]/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_decimal_public_literal_is_fetched():
    """134744074 is not private (decimal 8.8.8.10); fetch must proceed after DNS mapping."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"public-decimal",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        body, ext = asyncio.run(
            fetch_url_document("https://134744074/doc.txt", _url_import_settings())
        )
    assert body == b"public-decimal"
    assert ext == ".txt"
    assert requests == [("GET", "https://134744074/doc.txt")]


def test_fetch_deflate_content_encoding_stores_decoded_plaintext():
    """httpx aiter_bytes decodes zlib deflate; stored body must be plaintext, not compressed bytes."""
    plain = b"UniqueDeflatePlaintextMarker for stored document"
    compressed = zlib.compress(plain)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "deflate",
                "Content-Length": str(len(compressed)),
            },
            content=compressed,
        )

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    with patch("app.url_import.httpx.AsyncClient", factory), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/doc.txt",
                _url_import_settings(url_import_max_bytes=1024 * 1024),
            )
        )

    assert body == plain
    assert ext == ".txt"
    assert requests == ["https://example.com/doc.txt"]
