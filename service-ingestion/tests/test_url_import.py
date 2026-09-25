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
        "192.0.00002.1",  # extra-padded octal third octet TEST-NET-1
        "198.51.000144.1",  # extra-padded octal third octet TEST-NET-2
        "203.0.0000161.1",  # extra-padded octal third octet TEST-NET-3
    ],
)
def test_raise_for_private_blocks_extra_padded_octal_third_octet(host):
    """ipaddress rejects extra-padded octal third octets; glibc maps them to TEST-NET.

    00002 is 2, 000144 is 100, and 0000161 is 113. Distinct from shorter
    padding 192.0.0002.1 / 198.51.0144.1 / 203.0.000161.1.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.2.0001",  # extra-padded last-octet octal TEST-NET-1
        "198.51.100.0001",  # extra-padded last-octet octal TEST-NET-2
        "203.0.113.0001",  # extra-padded last-octet octal TEST-NET-3
    ],
)
def test_raise_for_private_blocks_extra_padded_last_octet_octal(host):
    """ipaddress rejects 0001 last-octet octal; glibc maps it to octet 1.

    Distinct from shorter last-octet octal 192.0.2.01 / 198.51.100.01 /
    203.0.113.01 and from extra-padded last-octet hex *.0x001.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_extra_padded_first_octet_hex_six_digits():
    """0x000000c0.0.2.1 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Distinct from shorter first-octet padding 0x0000c0.0.2.1 and 0x00c0.0.2.1.
    """
    host = "0x000000c0.0.2.1"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "203.0.0x0071.1",  # extra-padded third-octet hex TEST-NET-3
        "198.51.0x0064.1",  # extra-padded third-octet hex TEST-NET-2
    ],
)
def test_raise_for_private_blocks_extra_padded_third_octet_hex_test_net(host):
    """ipaddress rejects 0x0071 / 0x0064; glibc maps them to 113 and 100.

    Distinct from shorter padding 203.0.0x071.1 and 198.51.0x064.1.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "198.51.0x006401",  # extra-padded three-part 16-bit hex TEST-NET-2
        "203.0.0x007101",  # extra-padded three-part 16-bit hex TEST-NET-3
    ],
)
def test_raise_for_private_blocks_extra_padded_three_part_hex_test_net(host):
    """ipaddress rejects these three-part forms; glibc maps them to TEST-NET .1.

    0x006401 is 198.51.100.1 and 0x007101 is 203.0.113.1. Distinct from
    shorter three-part 203.0.0x7101 and from dotted 203.0.0x0071.1.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_mixed_octal_third_and_hex_last():
    """192.0.0002.0x1 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Distinct from octal-only 192.0.0002.1 and from hex-only 192.0.0x02.1.
    """
    host = "192.0.0002.0x1"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_extra_padded_hex_two_octet():
    """0x0000c0.0x0002 is an ipaddress miss; glibc maps it to 192.0.0.2.

    Distinct from shorter two-octet padding 0x00c0.0x0002.
    """
    host = "0x0000c0.0x0002"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_three_part_extra_padded_octal_loopback():
    """0177.0.0001 is an ipaddress miss; glibc maps it to 127.0.0.1.

    Distinct from three-part 0177.0.1 and from mixed 0177.1.
    """
    host = "0177.0.0001"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("url_host", "resolved_host"),
    [
        ("[::ffff:192.88.99.7]", "::ffff:192.88.99.7"),
        ("[::ffff:0:192.88.99.7]", "::ffff:0:192.88.99.7"),
        ("[64:ff9b::192.88.99.7]", "64:ff9b::192.88.99.7"),
        ("[2002:c058:6307::]", "2002:c058:6307::"),
    ],
)
def test_raise_for_private_blocks_6to4_relay_anycast_dot7_ipv6_encodings(
    url_host, resolved_host
):
    """6to4 relay anycast 192.88.99.7 IPv6 aliases are reserved or private.

    Mapped / SIIT / NAT64 are reserved and not private; dropping the
    reserved check would allow those fetches. 6to4 is private-only.
    Distinct from 192.88.99.6 and the earlier relay anycast hosts.
    Bare 192.88.99.7 is currently is_global.
    """
    _raw, host, _port = _parse_and_validate_url(f"https://{url_host}/secret")
    assert host == resolved_host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_fetch_extra_padded_octal_third_test_net_3_is_blocked_before_get():
    """203.0.0000161.1 is an ipaddress miss; glibc maps it to 203.0.113.1.

    Fetch must 403 before GET. Distinct from shorter octal 203.0.000161.1.
    """
    _assert_fetch_blocked_before_get("https://203.0.0000161.1/secret")


def test_fetch_three_part_octal_loopback_is_blocked_before_get():
    """0177.0.0001 is an ipaddress miss; glibc maps it to 127.0.0.1.

    Fetch must 403 before GET. Distinct from three-part 0177.0.1.
    """
    _assert_fetch_blocked_before_get("https://0177.0.0001/secret")


def test_fetch_mapped_6to4_relay_anycast_dot7_is_blocked_before_get():
    """[::ffff:192.88.99.7] is reserved and not private; fetch must 403 before GET.

    Distinct from mapped 192.88.99.6. Bare 192.88.99.7 is currently allowed;
    the mapped encoding is not.
    """
    _assert_fetch_blocked_before_get("https://[::ffff:192.88.99.7]/secret")
