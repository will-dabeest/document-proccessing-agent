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
        "0xc6336401",  # integer hex TEST-NET-2
        "3325256705",  # integer decimal TEST-NET-2
        "0306.063.0144.1",  # four-part octal TEST-NET-2
        "0xc6.0x33.0x64.1",  # four-part hex TEST-NET-2
    ],
)
def test_raise_for_private_blocks_integer_octal_test_net_2(host):
    """ipaddress rejects these; glibc getaddrinfo maps them to 198.51.100.1.

    Distinct from TEST-NET-2 IPv6 encodings (#97) and bare 198.51.100.1 (#72).
    Do not confuse decimal 3325256705 with nearby public integers.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xcb007101",  # integer hex TEST-NET-3
        "3405803777",  # integer decimal TEST-NET-3 (not 3405804801 → 203.0.117.1)
        "0313.0.0161.1",  # four-part octal TEST-NET-3
        "0xcb.0.0x71.1",  # four-part hex TEST-NET-3
    ],
)
def test_raise_for_private_blocks_integer_octal_test_net_3(host):
    """ipaddress rejects these; glibc maps 0xcb007101 / 3405803777 /
    0313.0.0161.1 / 0xcb.0.0x71.1 to 203.0.113.1.

    Distinct from TEST-NET-3 IPv6 encodings (#97) and bare 203.0.113.1 (#72).
    Decimal 3405804801 maps to public 203.0.117.1 and must not be used here.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "0xc0000001",  # integer hex IETF 192.0.0.1
        "3221225473",  # integer decimal IETF 192.0.0.1
        "0300.0.0.1",  # four-part octal IETF 192.0.0.1
        "0xc0.0.0.1",  # four-part hex IETF 192.0.0.1
        "192.0.0",  # two-octet → 192.0.0.0 (IETF 192.0.0.0/24, private)
    ],
)
def test_raise_for_private_blocks_integer_octal_ietf_protocol(host):
    """ipaddress rejects these; glibc maps 0xc0000001 / 3221225473 /
    0300.0.0.1 / 0xc0.0.0.1 to 192.0.0.1 and 192.0.0 to 192.0.0.0.

    Distinct from IETF IPv6 encodings (#97), integer TEST-NET-1 0xc0000201
    (#97), and four-part hex TEST-NET-1 0xc0.0.2.1 (#96). Two-octet 192.0.0
    is the IETF protocol network address, not TEST-NET-1.
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_fetch_integer_hex_test_net_2_is_blocked_before_get():
    """0xc6336401 is an ipaddress miss; glibc maps it to 198.51.100.1.

    Fetch must 403 before GET. Distinct from TEST-NET-2 IPv6 (#97).
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
                    "https://0xc6336401/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_integer_hex_test_net_3_is_blocked_before_get():
    """0xcb007101 is an ipaddress miss; glibc maps it to 203.0.113.1.

    Fetch must 403 before GET. Distinct from TEST-NET-3 IPv6 (#97).
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
                    "https://0xcb007101/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_integer_hex_ietf_protocol_is_blocked_before_get():
    """0xc0000001 is an ipaddress miss; glibc maps it to 192.0.0.1.

    Fetch must 403 before GET. Distinct from IETF IPv6 (#97) and
    integer-hex TEST-NET-1 0xc0000201 (#97).
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
                    "https://0xc0000001/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_two_octet_ietf_protocol_is_blocked_before_get():
    """192.0.0 is an ipaddress miss; glibc maps it to 192.0.0.0.

    Fetch must 403 before GET. Distinct from two-octet 192.0.2 (#97),
    which maps to 192.0.0.2 still inside 192.0.0.0/24.
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
                fetch_url_document("https://192.0.0/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []
