import asyncio
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
        "::ffff:255.255.255.255",  # IPv4-mapped broadcast: private+reserved
        "::ffff:0:255.255.255.255",  # SIIT broadcast: reserved-only
        "64:ff9b::255.255.255.255",  # NAT64 broadcast: reserved-only
        "2002:ffff:ffff::",  # 6to4 of 255.255.255.255: private, not reserved
    ],
)
def test_raise_for_private_blocks_broadcast_ipv6_encodings(host):
    """Broadcast 255.255.255.255 IPv6 aliases are not all private.

    Bare 255.255.255.255 is #70. Mapped is private+reserved; SIIT/NAT64
    are reserved-only; 6to4 is private-only. Dropping reserved would
    allow SIIT/NAT64 broadcast fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:240.0.0.1",  # IPv4-mapped Class E: private+reserved
        "::ffff:0:240.0.0.1",  # SIIT Class E: reserved-only
        "64:ff9b::240.0.0.1",  # NAT64 Class E: reserved-only
        "2002:f000:1::",  # 6to4 of 240.0.0.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_class_e_ipv6_encodings(host):
    """Class E 240.0.0.0/4 IPv6 aliases are not all private.

    Bare 240.0.0.1 is #70. Mapped is private+reserved; SIIT/NAT64 are
    reserved-only; 6to4 is private-only. Dropping reserved would allow
    SIIT/NAT64 Class E fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:192.0.2.1",  # IPv4-mapped TEST-NET-1: private+reserved
        "::ffff:0:192.0.2.1",  # SIIT TEST-NET-1: reserved-only
        "64:ff9b::192.0.2.1",  # NAT64 TEST-NET-1: reserved-only
        "2002:c000:201::",  # 6to4 of 192.0.2.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_test_net_1_ipv6_encodings(host):
    """TEST-NET-1 192.0.2.0/24 IPv6 aliases are not all private.

    Bare 192.0.2.1 is #72. Mapped is private+reserved; SIIT/NAT64 are
    reserved-only; 6to4 is private-only. Dropping reserved would allow
    SIIT/NAT64 documentation-range fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:192.88.99.1",  # IPv4-mapped 6to4 relay anycast: reserved-only
        "::ffff:0:192.88.99.1",  # SIIT 6to4 relay anycast: reserved-only
        "64:ff9b::192.88.99.1",  # NAT64 6to4 relay anycast: reserved-only
        "2002:c058:6301::",  # 6to4 of 192.88.99.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_6to4_relay_anycast_ipv6_encodings(host):
    """6to4 relay anycast 192.88.99.1 IPv6 aliases are reserved or private.

    Bare 192.88.99.1 is currently is_global (allowed; needs prod). Mapped
    / SIIT / NAT64 are reserved-only; 6to4 is private-only. Dropping
    reserved would allow mapped/SIIT/NAT64 fetches of the relay anycast.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_four_part_hex_broadcast():
    """ipaddress rejects 0xff.0xff.0xff.0xff; glibc maps it to 255.255.255.255.

    Distinct from bare dotted-decimal 255.255.255.255 (#70).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("0xff.0xff.0xff.0xff")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_four_part_hex_class_e():
    """ipaddress rejects 0xf0.0.0.1; glibc getaddrinfo maps it to 240.0.0.1.

    Distinct from bare 240.0.0.1 (#70) and four-part hex loopback
    0x7f.0.0.1 (#86).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("0xf0.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_four_part_hex_test_net_1():
    """ipaddress rejects 0xc0.0.2.1; glibc getaddrinfo maps it to 192.0.2.1.

    Distinct from bare TEST-NET-1 192.0.2.1 (#72) and four-part hex
    192.168 0xc0.0xa8.0.1 (#88).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("0xc0.0.2.1")
    assert exc.value.status_code == 403


def test_fetch_siit_broadcast_is_blocked_before_get():
    """[::ffff:0:255.255.255.255] is reserved-only SIIT; fetch must 403 before GET."""
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
                    "https://[::ffff:0:255.255.255.255]/secret",
                    _url_import_settings(),
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_nat64_class_e_is_blocked_before_get():
    """[64:ff9b::240.0.0.1] is reserved-only NAT64; fetch must 403 before GET."""
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
                    "https://[64:ff9b::240.0.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_siit_test_net_1_is_blocked_before_get():
    """[::ffff:0:192.0.2.1] is reserved-only SIIT; fetch must 403 before GET."""
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
                    "https://[::ffff:0:192.0.2.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_mapped_6to4_relay_anycast_is_blocked_before_get():
    """[::ffff:192.88.99.1] is reserved-only; fetch must 403 before GET.

    Bare 192.88.99.1 is currently allowed; the mapped encoding is not.
    """
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
                    "https://[::ffff:192.88.99.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []
