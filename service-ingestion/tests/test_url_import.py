import asyncio
import gzip
import socket
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
        "0xc0.0xa8.0.1",  # dotted-hex 192.168.0.1
        "0xc0a80001",  # integer-hex 192.168.0.1
        "3232235521",  # decimal 192.168.0.1
        "192.168.1",  # short-form 192.168.0.1
        "0177.1",  # octal + short-form loopback
    ],
)
def test_raise_for_private_blocks_rfc1918_192_168_encodings_and_mixed_loopback(host):
    """ipaddress rejects these; glibc getaddrinfo maps them to private/loopback."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:8.8.8.8",  # IPv4-mapped public (reserved)
        "169.254.1.1",  # IPv4 link-local
        "2002:c0a8:1::",  # 6to4 wrapping 192.168.0.1
    ],
)
def test_raise_for_private_blocks_mapped_link_local_and_6to4_rfc1918_literals(host):
    """These parse as IP literals; SSRF checks must not skip DNS and then allow them."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_allows_hex_last_octet_public_literal():
    """ipaddress rejects 8.8.8.0xa; glibc maps it to public 8.8.8.10."""
    raise_for_private_or_meta_hosts("8.8.8.0xa")


@pytest.mark.parametrize(
    "content_type",
    [
        "image/bmp",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
def test_classify_bmp_and_office_mime_are_unknown_binary(content_type):
    """URL import allow-list is HTML/PDF/text/markdown; BMP and Office MIME must 415."""
    assert _classify_body(content_type, b"not-a-document") == "unknown_binary"


def test_fetch_dotted_hex_rfc1918_192_168_is_blocked_before_get():
    """Dotted-hex 0xc0.0xa8.0.1 is 192.168.0.1 via getaddrinfo; fetch must 403 before GET."""
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
                fetch_url_document(
                    "https://0xc0.0xa8.0.1/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_mixed_octal_short_loopback_is_blocked_before_get():
    """0177.1 is 127.0.0.1 via getaddrinfo; fetch must 403 before GET."""
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
                fetch_url_document("https://0177.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_ipv4_mapped_public_literal_is_blocked_without_dns():
    """::ffff:8.8.8.8 is reserved/mapped; must 403 as a literal (no DNS, no GET)."""
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
                fetch_url_document(
                    "https://[::ffff:8.8.8.8]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_hex_last_octet_public_literal_is_fetched():
    """8.8.8.0xa is not private (hex 0xa == 10); fetch must proceed after DNS mapping."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"public-hex-last",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        body, ext = asyncio.run(
            fetch_url_document("https://8.8.8.0xa/doc.txt", _url_import_settings())
        )
    assert body == b"public-hex-last"
    assert ext == ".txt"
    assert requests == [("GET", "https://8.8.8.0xa/doc.txt")]


def test_fetch_rejects_bmp_content_type():
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "image/bmp"},
                chunks=(b"BM-not-a-document",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/a.bmp", _url_import_settings())
            )
    assert exc.value.status_code == 415
    assert requests == [("GET", "https://example.com/a.bmp")]


def test_fetch_gzip_content_encoding_stores_decoded_plaintext():
    """httpx aiter_bytes decodes gzip; stored body must be plaintext, not compressed bytes."""
    plain = b"UniqueGzipPlaintextMarker for stored document"
    compressed = gzip.compress(plain)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "gzip",
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
