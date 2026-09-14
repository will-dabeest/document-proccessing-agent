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
        "::ffff:0.0.0.1",  # IPv4-mapped this-network: private+reserved
        "::ffff:0:0.0.0.1",  # SIIT this-network: reserved-only
        "64:ff9b::0.0.0.1",  # NAT64 this-network: reserved-only
        "2002:0:1::",  # 6to4 of 0.0.0.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_this_network_ipv6_encodings(host):
    """This-network 0.0.0.0/8 IPv6 aliases are not all private.

    Bare 0.0.0.1 is #89. Mapped is private+reserved; SIIT/NAT64 are
    reserved-only; 6to4 is private-only. Dropping reserved would allow
    SIIT/NAT64 this-network fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:0:0.0.0.0",  # SIIT unspecified: reserved-only
        "64:ff9b::",  # NAT64 unspecified (0.0.0.0): reserved-only
        "::ffff:0.0.0.0",  # IPv4-mapped unspecified: private+reserved
        "2002::",  # 6to4 of 0.0.0.0: private, not reserved
    ],
)
def test_raise_for_private_blocks_unspecified_ipv6_encodings(host):
    """Unspecified 0.0.0.0 IPv6 aliases fail different flag combinations.

    Bare 0.0.0.0 is already private. SIIT/NAT64 are reserved-only (not
    private); mapped is private+reserved; 6to4 is private-only.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:0:198.18.0.1",  # SIIT benchmarking: reserved-only
        "64:ff9b::198.18.0.1",  # NAT64 benchmarking: reserved-only
        "2002:c612:1::",  # 6to4 of 198.18.0.1: private, not reserved
    ],
)
def test_raise_for_private_blocks_benchmark_ipv6_encodings(host):
    """Benchmark 198.18/15 IPv6 aliases are not all private.

    Bare 198.18.0.1 is already private. SIIT/NAT64 are reserved-only;
    6to4 is private-only. Dropping reserved would allow SIIT/NAT64
    benchmarking-range fetches.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:224.0.0.1",  # IPv4-mapped multicast: reserved, not multicast
        "::ffff:0:224.0.0.1",  # SIIT multicast: reserved-only
        "64:ff9b::224.0.0.1",  # NAT64 multicast: reserved-only
        "2002:e000:1::",  # 6to4 of 224.0.0.1: private, not reserved/multicast
    ],
)
def test_raise_for_private_blocks_multicast_ipv6_encodings(host):
    """IPv4 multicast IPv6 aliases are not is_multicast.

    Bare 224.0.0.1 is #57. Mapped/SIIT/NAT64 are reserved-only and not
    multicast; 6to4 is private-only. A multicast-only check would miss
    every encoding here.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_three_part_short_hex_rfc1918_10():
    """ipaddress rejects 0xa.0.1; glibc getaddrinfo maps it to 10.0.0.1.

    Distinct from four-part short-hex 0xa.0.0.1 (#92) and three-part
    mixed hex 0xc0.0xa8.1 (#94).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("0xa.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_three_part_octal_rfc1918_10():
    """ipaddress rejects 012.0.1; glibc getaddrinfo maps it to 10.0.0.1.

    Distinct from four-part octal 010.0.0.1 (#86, public 8.0.0.1) and
    two-part 10.1 (#87).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("012.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_three_part_mixed_octal_rfc1918_172_16():
    """ipaddress rejects 0254.16.1; glibc getaddrinfo maps it to 172.16.0.1.

    Distinct from four-part 0254.020.0.1 (#91) and three-part 0254.020.1
    (#94).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("0254.16.1")
    assert exc.value.status_code == 403


def test_fetch_siit_this_network_is_blocked_before_get():
    """[::ffff:0:0.0.0.1] is reserved-only SIIT; fetch must 403 before GET."""
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
                    "https://[::ffff:0:0.0.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_nat64_unspecified_is_blocked_before_get():
    """[64:ff9b::] is reserved-only NAT64 of 0.0.0.0; fetch must 403 before GET."""
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
                fetch_url_document("https://[64:ff9b::]/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_mapped_multicast_is_blocked_before_get():
    """[::ffff:224.0.0.1] is reserved, not multicast; fetch must 403 before GET."""
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
                    "https://[::ffff:224.0.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_three_part_short_hex_rfc1918_10_is_blocked_before_get():
    """0xa.0.1 is 10.0.0.1 via getaddrinfo; fetch must 403 before GET."""
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
                fetch_url_document("https://0xa.0.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []
