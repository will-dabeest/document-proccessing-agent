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
        "0xc6.51.100.1",  # mixed first-octet hex TEST-NET-2
        "0306.51.100.1",  # mixed first-octet octal TEST-NET-2
    ],
)
def test_raise_for_private_blocks_mixed_first_octet_test_net_2(host):
    """ipaddress rejects these; glibc maps 0xc6.51.100.1 / 0306.51.100.1
    to 198.51.100.1.

    Distinct from four-part hex 0xc6.0x33.0x64.1 and integer-hex
    0xc6336401 (#98), TEST-NET-2 IPv6 (#97), and bare 198.51.100.1 (#72).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xcb.0.113.1",  # mixed first-octet hex TEST-NET-3
        "203.0.0161.1",  # last-octet octal TEST-NET-3
    ],
)
def test_raise_for_private_blocks_mixed_test_net_3(host):
    """ipaddress rejects these; glibc maps 0xcb.0.113.1 / 203.0.0161.1
    to 203.0.113.1.

    Distinct from four-part hex 0xcb.0.0x71.1 and integer-hex 0xcb007101
    (#98), TEST-NET-3 IPv6 (#97), and bare 203.0.113.1 (#72).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xc0000000",  # integer hex IETF network 192.0.0.0
        "3221225472",  # integer decimal IETF network 192.0.0.0
        "192.0",  # two-octet → 192.0.0.0
    ],
)
def test_raise_for_private_blocks_integer_ietf_network(host):
    """ipaddress rejects these; glibc maps 0xc0000000 / 3221225472 /
    192.0 to 192.0.0.0.

    Distinct from integer-hex IETF host 0xc0000001 and three-octet
    192.0.0 (#98), IETF IPv6 (#97), and integer TEST-NET-1 0xc0000201
    (#97). Two-octet 192.0 is the IETF protocol network, not TEST-NET-1.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:192.88.99.2",  # IPv4-mapped 6to4 relay anycast .2: reserved-only
        "::ffff:0:192.88.99.2",  # SIIT 6to4 relay anycast .2: reserved-only
        "64:ff9b::192.88.99.2",  # NAT64 6to4 relay anycast .2: reserved-only
        "2002:c058:6302::",  # 6to4 of 192.88.99.2: private, not reserved
    ],
)
def test_raise_for_private_blocks_6to4_relay_anycast_dot2_ipv6_encodings(host):
    """6to4 relay anycast 192.88.99.2 IPv6 aliases are reserved or private.

    Distinct from 192.88.99.1 encodings (#96). Bare 192.88.99.2 is currently
    is_global (allowed; needs prod). Mapped / SIIT / NAT64 are reserved-only;
    6to4 is private-only. Dropping reserved would allow mapped/SIIT/NAT64
    fetches of other 192.88.99.0/24 relay addresses.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_fetch_mixed_hex_test_net_2_is_blocked_before_get():
    """0xc6.51.100.1 is an ipaddress miss; glibc maps it to 198.51.100.1.

    Fetch must 403 before GET. Distinct from integer-hex 0xc6336401 (#98)
    and TEST-NET-2 IPv6 (#97).
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
                    "https://0xc6.51.100.1/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_mixed_hex_test_net_3_is_blocked_before_get():
    """0xcb.0.113.1 is an ipaddress miss; glibc maps it to 203.0.113.1.

    Fetch must 403 before GET. Distinct from integer-hex 0xcb007101 (#98)
    and TEST-NET-3 IPv6 (#97).
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
                    "https://0xcb.0.113.1/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_integer_hex_ietf_network_is_blocked_before_get():
    """0xc0000000 is an ipaddress miss; glibc maps it to 192.0.0.0.

    Fetch must 403 before GET. Distinct from IETF host 0xc0000001 (#98)
    and two-octet 192.0.0 (#98).
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
                    "https://0xc0000000/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_two_octet_ietf_network_is_blocked_before_get():
    """192.0 is an ipaddress miss; glibc maps it to 192.0.0.0.

    Fetch must 403 before GET. Distinct from three-octet 192.0.0 (#98)
    and two-octet 192.0.2 (#97).
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
                fetch_url_document("https://192.0/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_mapped_6to4_relay_anycast_dot2_is_blocked_before_get():
    """[::ffff:192.88.99.2] is reserved-only; fetch must 403 before GET.

    Distinct from mapped 192.88.99.1 (#96). Bare 192.88.99.2 is currently
    allowed; the mapped encoding is not.
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
                    "https://[::ffff:192.88.99.2]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []
