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
        "::ffff:198.51.100.1",  # IPv4-mapped TEST-NET-2: private+reserved
        "::ffff:0:198.51.100.1",  # SIIT TEST-NET-2: reserved-only
        "64:ff9b::198.51.100.1",  # NAT64 TEST-NET-2: reserved-only
        "2002:c633:6401::",  # 6to4 of 198.51.100.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_test_net_2_ipv6_encodings(host):
    """TEST-NET-2 198.51.100.0/24 IPv6 aliases are not all private.

    Bare 198.51.100.1 is #72. Mapped is private+reserved; SIIT/NAT64 are
    reserved-only; 6to4 is private-only. Dropping reserved would allow
    SIIT/NAT64 documentation-range fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:203.0.113.1",  # IPv4-mapped TEST-NET-3: private+reserved
        "::ffff:0:203.0.113.1",  # SIIT TEST-NET-3: reserved-only
        "64:ff9b::203.0.113.1",  # NAT64 TEST-NET-3: reserved-only
        "2002:cb00:7101::",  # 6to4 of 203.0.113.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_test_net_3_ipv6_encodings(host):
    """TEST-NET-3 203.0.113.0/24 IPv6 aliases are not all private.

    Bare 203.0.113.1 is #72. Mapped is private+reserved; SIIT/NAT64 are
    reserved-only; 6to4 is private-only. Dropping reserved would allow
    SIIT/NAT64 documentation-range fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:192.0.0.1",  # IPv4-mapped IETF 192.0.0.0/24: private+reserved
        "::ffff:0:192.0.0.1",  # SIIT IETF 192.0.0.0/24: reserved-only
        "64:ff9b::192.0.0.1",  # NAT64 IETF 192.0.0.0/24: reserved-only
        "2002:c000:1::",  # 6to4 of 192.0.0.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_ietf_protocol_ipv6_encodings(host):
    """IETF protocol assignments 192.0.0.0/24 IPv6 aliases are not all private.

    Bare 192.0.0.1 is already private. Mapped is private+reserved;
    SIIT/NAT64 are reserved-only; 6to4 is private-only. Dropping reserved
    would allow SIIT/NAT64 fetches of the IETF protocol range.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xffffffff",  # integer hex broadcast
        "4294967295",  # integer decimal broadcast
        "0377.0377.0377.0377",  # four-part octal broadcast
    ],
)
def test_raise_for_private_blocks_integer_octal_broadcast(host):
    """ipaddress rejects these; glibc getaddrinfo maps them to 255.255.255.255.

    Distinct from four-part hex 0xff.0xff.0xff.0xff (#96) and bare
    dotted-decimal 255.255.255.255 (#70).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xf0000001",  # integer hex Class E
        "4026531841",  # integer decimal Class E
        "0360.0.0.1",  # four-part octal Class E
        "240.0.1",  # three-part decimal Class E
    ],
)
def test_raise_for_private_blocks_integer_octal_class_e(host):
    """ipaddress rejects these; glibc getaddrinfo maps them to 240.0.0.1.

    Distinct from four-part hex 0xf0.0.0.1 (#96) and bare 240.0.0.1 (#70).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xc0000201",  # integer hex TEST-NET-1
        "3221225985",  # integer decimal TEST-NET-1
        "0300.0.2.1",  # four-part octal TEST-NET-1
        "192.0.2",  # two-octet → 192.0.0.2 (IETF 192.0.0.0/24, private)
    ],
)
def test_raise_for_private_blocks_integer_octal_test_net_1(host):
    """ipaddress rejects these; glibc maps 0xc0000201 / 3221225985 /
    0300.0.2.1 to 192.0.2.1 and 192.0.2 to 192.0.0.2.

    Distinct from four-part hex 0xc0.0.2.1 (#96) and bare 192.0.2.1 (#72).
    Two-octet 192.0.2 is the IETF protocol range, not TEST-NET-1.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_fetch_siit_test_net_2_is_blocked_before_get():
    """[::ffff:0:198.51.100.1] is reserved-only SIIT; fetch must 403 before GET."""
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
                    "https://[::ffff:0:198.51.100.1]/secret",
                    _url_import_settings(),
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_nat64_test_net_3_is_blocked_before_get():
    """[64:ff9b::203.0.113.1] is reserved-only NAT64; fetch must 403 before GET."""
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
                    "https://[64:ff9b::203.0.113.1]/secret",
                    _url_import_settings(),
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_siit_ietf_protocol_is_blocked_before_get():
    """[::ffff:0:192.0.0.1] is reserved-only SIIT; fetch must 403 before GET."""
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
                    "https://[::ffff:0:192.0.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_integer_hex_broadcast_is_blocked_before_get():
    """0xffffffff is an ipaddress miss; glibc maps it to 255.255.255.255.

    Fetch must 403 before GET. Distinct from four-part hex
    0xff.0xff.0xff.0xff (#96).
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
                    "https://0xffffffff/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []
