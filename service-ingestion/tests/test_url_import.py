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
        "192.0.2.0x01",  # last-octet padded-hex TEST-NET-1
        "198.51.100.0x01",  # last-octet padded-hex TEST-NET-2
        "203.0.113.0x01",  # last-octet padded-hex TEST-NET-3
        "192.0.0.0x01",  # last-octet padded-hex IETF host
    ],
)
def test_raise_for_private_blocks_last_octet_padded_hex_ietf_and_test_net(host):
    """ipaddress rejects 0x01 last octets; glibc maps them to IETF/TEST-NET.

    Distinct from unpadded last-octet hex 192.0.2.0x1 / 198.51.100.0x1 /
    203.0.113.0x1 / 192.0.0.0x1 (#102 / #101) and last-octet padded-octal
    *.01 (#102).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "203.0.0x71.0x1",  # mixed last-two-octet hex TEST-NET-3
        "192.0.0x2.0x1",  # mixed last-two-octet hex TEST-NET-1
    ],
)
def test_raise_for_private_blocks_mixed_last_two_octet_hex_test_net_1_and_3(host):
    """ipaddress rejects these; glibc maps them to 203.0.113.1 and 192.0.2.1.

    Distinct from TEST-NET-2 last-two-octet hex 198.51.0x64.0x1 (#102),
    third-octet-only 203.0.0x71.1 (#102), and last-octet-only hex (#101).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "198.51.0x64.01",  # third-octet hex + last-octet octal TEST-NET-2
        "203.0.0x71.01",  # third-octet hex + last-octet octal TEST-NET-3
    ],
)
def test_raise_for_private_blocks_mixed_hex_and_padded_octal_test_net(host):
    """ipaddress rejects mixed hex/octal; glibc maps them to TEST-NET hosts.

    Distinct from all-hex last-two-octet 198.51.0x64.0x1 / 203.0.0x71.0x1
    and from last-octet padded-octal 198.51.100.01 / 203.0.113.01 (#102).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xc0.0x02",  # two-octet padded-hex of 192.0.0.2
        "0300.02",  # two-octet padded-octal of 192.0.0.2
    ],
)
def test_raise_for_private_blocks_padded_two_octet_test_net_1(host):
    """ipaddress rejects these; glibc maps them to 192.0.0.2.

    Distinct from unpadded two-octet 0xc0.0x2 / 0xc0.02 / 0300.0x2 (#102)
    and from 0300.2 / 0xc0.2 (#101).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "::ffff:192.88.99.4",  # IPv4-mapped 6to4 relay anycast .4: reserved, not private
        "::ffff:0:192.88.99.4",  # SIIT 6to4 relay anycast .4: reserved, not private
        "64:ff9b::192.88.99.4",  # NAT64 6to4 relay anycast .4: reserved, not private
        "2002:c058:6304::",  # 6to4 of 192.88.99.4: private, not reserved
    ],
)
def test_raise_for_private_blocks_6to4_relay_anycast_dot4_ipv6_encodings(host):
    """6to4 relay anycast 192.88.99.4 IPv6 aliases are reserved or private.

    Distinct from 192.88.99.0 (#101), .1 (#96), .2 (#99), .3 (#102), and
    .255 (#100). Mapped / SIIT / NAT64 are reserved and not private;
    dropping the reserved check would allow those fetches. 6to4 is
    private-only. Bare 192.88.99.4 is currently is_global (allowed).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


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


def test_fetch_last_octet_padded_hex_test_net_1_is_blocked_before_get():
    """192.0.2.0x01 is an ipaddress miss; glibc maps it to 192.0.2.1.

    Fetch must 403 before GET. Distinct from unpadded 192.0.2.0x1 (#102).
    """
    _assert_fetch_blocked_before_get("https://192.0.2.0x01/secret")


def test_fetch_mixed_last_two_octet_hex_test_net_3_is_blocked_before_get():
    """203.0.0x71.0x1 is an ipaddress miss; glibc maps it to 203.0.113.1.

    Fetch must 403 before GET. Distinct from third-octet-only
    203.0.0x71.1 (#102) and TEST-NET-2 198.51.0x64.0x1 (#102).
    """
    _assert_fetch_blocked_before_get("https://203.0.0x71.0x1/secret")


def test_fetch_mixed_hex_octal_test_net_2_is_blocked_before_get():
    """198.51.0x64.01 is an ipaddress miss; glibc maps it to 198.51.100.1.

    Fetch must 403 before GET. Distinct from all-hex 198.51.0x64.0x1
    (#102) and last-octet padded-octal 198.51.100.01 (#102).
    """
    _assert_fetch_blocked_before_get("https://198.51.0x64.01/secret")


def test_fetch_padded_two_octet_hex_test_net_1_is_blocked_before_get():
    """0xc0.0x02 is an ipaddress miss; glibc maps it to 192.0.0.2.

    Fetch must 403 before GET. Distinct from unpadded 0xc0.0x2 (#102).
    """
    _assert_fetch_blocked_before_get("https://0xc0.0x02/secret")


def test_fetch_mapped_6to4_relay_anycast_dot4_is_blocked_before_get():
    """[::ffff:192.88.99.4] is reserved and not private; fetch must 403 before GET.

    Distinct from mapped 192.88.99.3 (#102) and the other 6to4 relay
    anycast hosts already covered. Bare 192.88.99.4 is currently allowed;
    the mapped encoding is not.
    """
    _assert_fetch_blocked_before_get("https://[::ffff:192.88.99.4]/secret")
