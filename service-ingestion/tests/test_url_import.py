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


def test_raise_for_private_blocks_padded_third_octet_hex_test_net_3():
    """203.0.0x071.1 is an ipaddress miss; glibc maps it to 203.0.113.1.

    Distinct from padded third-octet hex 192.0.0x02.1 / 198.51.0x064.1
    and from three-part 16-bit hex 203.0.0x7101.
    """
    host = "203.0.0x071.1"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "198.51.100.0x001",  # extra-padded last-octet hex TEST-NET-2
        "203.0.113.0x001",  # extra-padded last-octet hex TEST-NET-3
        "192.0.0.0x001",  # extra-padded last-octet hex IETF protocol assignment
    ],
)
def test_raise_for_private_blocks_extra_padded_last_octet_hex(host):
    """ipaddress rejects 0x001 last-octet padding; glibc maps it to octet 1.

    Distinct from TEST-NET-1 padding 192.0.2.0x001 and from shorter
    192.0.2.0x01 / unpadded 192.0.2.0x1.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_extra_padded_first_octet_hex_test_net_1():
    """0x0000c0.0.2.1 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Distinct from shorter first-octet padding 0x00c0.0.2.1 and unpadded 0xc0.0.2.1.
    """
    host = "0x0000c0.0.2.1"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0x0002.1",  # extra-padded third-octet hex TEST-NET-1
        "192.0.0x0201",  # extra-padded three-part 16-bit hex TEST-NET-1
    ],
)
def test_raise_for_private_blocks_extra_padded_third_and_three_part_hex(host):
    """ipaddress rejects these extra-padded hex forms; glibc maps them to 192.0.2.1.

    Distinct from shorter padding 192.0.0x02.1 and three-part 192.0.0x201.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0300.0x0002",  # extra-padded mixed two-octet of 192.0.0.2
        "0x00c0.0x0002",  # extra-padded hex two-octet of 192.0.0.2
    ],
)
def test_raise_for_private_blocks_extra_padded_mixed_two_octet(host):
    """ipaddress rejects these two-octet forms; glibc maps them to 192.0.0.2.

    Distinct from 0300.0x02 / 0xc0.0x0002 / 0x00c0.0.2.1.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0002.1",  # padded octal third octet TEST-NET-1
        "198.51.0144.1",  # padded octal third octet TEST-NET-2
        "203.0.000161.1",  # padded octal third octet TEST-NET-3
    ],
)
def test_raise_for_private_blocks_padded_octal_third_octet(host):
    """ipaddress rejects padded octal third octets; glibc maps them to TEST-NET.

    0002 is 2 and 0144 / 000161 are 100 / 113. Distinct from hex padding
    192.0.0x02.1 and from last-octet octal forms.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("url_host", "resolved_host"),
    [
        ("[::ffff:192.88.99.6]", "::ffff:192.88.99.6"),
        ("[::ffff:0:192.88.99.6]", "::ffff:0:192.88.99.6"),
        ("[64:ff9b::192.88.99.6]", "64:ff9b::192.88.99.6"),
        ("[2002:c058:6306::]", "2002:c058:6306::"),
    ],
)
def test_raise_for_private_blocks_6to4_relay_anycast_dot6_ipv6_encodings(
    url_host, resolved_host
):
    """6to4 relay anycast 192.88.99.6 IPv6 aliases are reserved or private.

    Mapped / SIIT / NAT64 are reserved and not private; dropping the
    reserved check would allow those fetches. 6to4 is private-only.
    Distinct from 192.88.99.5 and the earlier relay anycast hosts.
    Bare 192.88.99.6 is currently is_global.
    """
    _raw, host, _port = _parse_and_validate_url(f"https://{url_host}/secret")
    assert host == resolved_host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_fetch_padded_third_octet_hex_test_net_3_is_blocked_before_get():
    """203.0.0x071.1 is an ipaddress miss; glibc maps it to 203.0.113.1.

    Fetch must 403 before GET. urlparse keeps the padded hex host intact.
    """
    _assert_fetch_blocked_before_get("https://203.0.0x071.1/secret")


def test_fetch_extra_padded_mixed_two_octet_is_blocked_before_get():
    """0x00c0.0x0002 is an ipaddress miss; glibc maps it to 192.0.0.2.

    Fetch must 403 before GET. Distinct from shorter padding 0xc0.0x0002.
    """
    _assert_fetch_blocked_before_get("https://0x00c0.0x0002/secret")


def test_fetch_mapped_6to4_relay_anycast_dot6_is_blocked_before_get():
    """[::ffff:192.88.99.6] is reserved and not private; fetch must 403 before GET.

    Distinct from mapped 192.88.99.5. Bare 192.88.99.6 is currently allowed;
    the mapped encoding is not.
    """
    _assert_fetch_blocked_before_get("https://[::ffff:192.88.99.6]/secret")
