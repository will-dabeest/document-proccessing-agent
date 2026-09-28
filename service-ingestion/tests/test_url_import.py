import asyncio
import ipaddress
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


def _assert_fetch_blocked_before_get(url: str) -> None:
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
            asyncio.run(fetch_url_document(url, _url_import_settings()))
    assert exc.value.status_code == 403
    assert requests == []


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0253",  # three-part octal -> 192.0.0.171
        "192.0.0xaa",  # three-part hex -> 192.0.0.170
        "192.0.0xab",  # three-part hex -> 192.0.0.171
        "0300.0.0252",  # octal network + octal host -> 192.0.0.170
        "0xc00000ab",  # hex integer -> 192.0.0.171
        "192.0.0.000252",  # extra-padded octal last octet -> 192.0.0.170
    ],
)
def test_raise_for_private_blocks_remaining_nat64_discovery_encodings(host):
    """RFC 7050 192.0.0.170/31 spellings that ipaddress rejects and glibc still maps.

    Four-part 192.0.0.0xaa / 192.0.0.0252 and hex integer 0xc00000aa are a
    separate set. These forms are the other /31 end, three-part hex, mixed
    octal, and one extra zero of last-octet padding.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(ValueError):
        ipaddress.ip_address(parsed_host)
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.000002.1",  # extra-padded octal third -> 192.0.2.1
        "198.51.0000144.1",  # extra-padded octal third -> 198.51.100.1
        "203.0.00000161.1",  # extra-padded octal third -> 203.0.113.1
        "192.0.2.00001",  # extra-padded octal last -> 192.0.2.1
        "198.51.100.00001",  # extra-padded octal last -> 198.51.100.1
        "203.0.113.00001",  # extra-padded octal last -> 203.0.113.1
        "192.0.0.0001",  # extra-padded octal last -> 192.0.0.1
        "0x00000000c0.0.2.1",  # extra-padded hex first -> 192.0.2.1
        "203.0.0x00071.1",  # extra-padded hex third -> 203.0.113.1
        "198.51.0x00064.1",  # extra-padded hex third -> 198.51.100.1
        "198.51.0x0006401",  # extra-padded hex 16-bit third -> 198.51.100.1
        "203.0.0x0007101",  # extra-padded hex 16-bit third -> 203.0.113.1
        "192.0.00002.0x1",  # extra-padded octal third + hex last -> 192.0.2.1
        "0x000000c0.0x0002",  # extra-padded two-part hex -> 192.0.0.2
        "0300.00002",  # two-part octal -> 192.0.0.2
        "0177.0.00001",  # extra-padded octal loopback -> 127.0.0.1
    ],
)
def test_raise_for_private_blocks_extra_padded_testnet_and_special_hosts(host):
    """One more zero of padding on TEST-NET, 192.0.0.0/24, and loopback spellings.

    ipaddress does not parse them. glibc still maps them onto reserved
    documentation networks, the private 192.0.0.0/24, or loopback.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(ValueError):
        ipaddress.ip_address(parsed_host)
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("url_host", "is_private", "is_reserved"),
    [
        ("[::ffff:192.88.99.8]", False, True),
        ("[::ffff:0:192.88.99.8]", False, True),
        ("[2002:c058:6308::]", True, False),
        ("[64:ff9b::192.88.99.8]", False, True),
    ],
)
def test_raise_for_private_blocks_ipv6_aliases_of_6to4_relay_8(
    url_host, is_private, is_reserved
):
    """IPv6 spellings of 6to4 relay 192.88.99.8 stay blocked after bracket stripping.

    Mapped and NAT64 forms are reserved and still globally reachable, so the
    check has to honor is_reserved. 6to4 2002:c058:6308:: is private.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{url_host}/secret")
    address = ipaddress.ip_address(parsed_host)
    assert address.is_private is is_private
    assert address.is_reserved is is_reserved
    if not is_private:
        assert address.is_global
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_fetch_three_part_hex_nat64_discovery_is_blocked_before_get():
    """192.0.0xaa is an ipaddress miss; glibc maps it to 192.0.0.170."""
    _assert_fetch_blocked_before_get("https://192.0.0xaa/secret")


def test_fetch_extra_padded_loopback_is_blocked_before_get():
    """0177.0.00001 is an ipaddress miss; glibc maps it to 127.0.0.1."""
    _assert_fetch_blocked_before_get("https://0177.0.00001/secret")


def test_fetch_reserved_mapped_6to4_relay_8_is_blocked_before_get():
    """::ffff:0:192.88.99.8 is reserved and not private. Fetch must still 403 before GET."""
    _assert_fetch_blocked_before_get("https://[::ffff:0:192.88.99.8]/secret")
