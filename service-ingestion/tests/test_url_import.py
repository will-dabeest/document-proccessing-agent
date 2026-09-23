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
        "192.0.0x201",  # three-part 16-bit hex TEST-NET-1
        "203.0.0x7101",  # three-part 16-bit hex TEST-NET-3
        "198.51.0x6401",  # three-part 16-bit hex TEST-NET-2
    ],
)
def test_raise_for_private_blocks_three_part_16bit_hex_test_net(host):
    """ipaddress rejects three-part 16-bit hex; glibc maps them to TEST-NET.

    Distinct from four-part mixed last-two-octet hex 192.0.0x2.0x1 /
    203.0.0x71.0x1 / 198.51.0x64.0x1 and from padded third-octet hex.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0x02.1",  # padded third-octet hex TEST-NET-1
        "198.51.0x064.1",  # padded third-octet hex TEST-NET-2
    ],
)
def test_raise_for_private_blocks_padded_third_octet_hex_test_net(host):
    """ipaddress rejects padded third-octet hex; glibc maps them to TEST-NET.

    Distinct from unpadded third-octet hex 192.0.0x2.1 / 198.51.0x64.1
    and from three-part 16-bit hex 192.0.0x201 / 198.51.0x6401.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xc0.0x0002",  # extra-padded two-octet hex of 192.0.0.2
        "0300.0002",  # extra-padded two-octet octal of 192.0.0.2
        "0300.0x02",  # mixed extra-padded two-octet of 192.0.0.2
    ],
)
def test_raise_for_private_blocks_extra_padded_two_octet_test_net_1(host):
    """ipaddress rejects extra-padded two-octet forms; glibc maps them to 192.0.0.2.

    Distinct from shorter padding 0xc0.0x02 / 0300.02 and from 0xc0.0x2 /
    0300.0x2 / 0xc0.02.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_padded_first_octet_hex_test_net_1():
    """0x00c0.0.2.1 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Distinct from unpadded first-octet hex 0xc0.0.2.1.
    """
    host = "0x00c0.0.2.1"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_mixed_octal_third_and_hex_last_test_net_1():
    """192.0.02.0x1 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Distinct from last-octet-only hex 192.0.2.0x1 and from third-octet-only
    padded hex 192.0.0x02.1.
    """
    host = "192.0.02.0x1"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_extra_padded_last_octet_hex_test_net_1():
    """192.0.2.0x001 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Distinct from shorter padding 192.0.2.0x01 and unpadded 192.0.2.0x1.
    """
    host = "192.0.2.0x001"
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("url_host", "resolved_host"),
    [
        ("[::ffff:192.88.99.5]", "::ffff:192.88.99.5"),
        ("[::ffff:0:192.88.99.5]", "::ffff:0:192.88.99.5"),
        ("[64:ff9b::192.88.99.5]", "64:ff9b::192.88.99.5"),
        ("[2002:c058:6305::]", "2002:c058:6305::"),
    ],
)
def test_raise_for_private_blocks_6to4_relay_anycast_dot5_ipv6_encodings(
    url_host, resolved_host
):
    """6to4 relay anycast 192.88.99.5 IPv6 aliases are reserved or private.

    Mapped / SIIT / NAT64 are reserved and not private; dropping the
    reserved check would allow those fetches. 6to4 is private-only.
    Distinct from 192.88.99.4 and the earlier relay anycast hosts.
    Bare 192.88.99.5 is currently is_global.
    """
    _raw, host, _port = _parse_and_validate_url(f"https://{url_host}/secret")
    assert host == resolved_host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_fetch_three_part_16bit_hex_test_net_1_is_blocked_before_get():
    """192.0.0x201 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Fetch must 403 before GET. urlparse keeps the three-part host intact.
    """
    _assert_fetch_blocked_before_get("https://192.0.0x201/secret")


def test_fetch_extra_padded_two_octet_hex_is_blocked_before_get():
    """0xc0.0x0002 is an ipaddress miss; glibc maps it to 192.0.0.2.

    Fetch must 403 before GET. Distinct from shorter padding 0xc0.0x02.
    """
    _assert_fetch_blocked_before_get("https://0xc0.0x0002/secret")


def test_fetch_mapped_6to4_relay_anycast_dot5_is_blocked_before_get():
    """[::ffff:192.88.99.5] is reserved and not private; fetch must 403 before GET.

    Distinct from mapped 192.88.99.4. Bare 192.88.99.5 is currently allowed;
    the mapped encoding is not.
    """
    _assert_fetch_blocked_before_get("https://[::ffff:192.88.99.5]/secret")
