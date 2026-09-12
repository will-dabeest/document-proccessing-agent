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
        "::ffff:0:10.0.0.1",  # SIIT of RFC1918 10/8
        "::ffff:0:172.16.0.1",  # SIIT of RFC1918 172.16/12
        "::ffff:0:192.168.0.1",  # SIIT of RFC1918 192.168/16
    ],
)
def test_raise_for_private_blocks_siit_rfc1918_literals(host):
    """SIIT RFC1918 is is_reserved but not is_private; reserved check is load-bearing.

    Distinct from IPv4-mapped ::ffff:x.x.x.x (#92) and SIIT loopback (#86).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_nat64_rfc1918_192_168():
    """NAT64 of 192.168.0.1 is reserved-only (not private); 10/172.16 NAT64 is #92."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("64:ff9b::192.168.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_three_part_mixed_rfc1918_192_168():
    """ipaddress rejects 0xc0.168.1; glibc getaddrinfo maps it to 192.168.0.1.

    Distinct from four-part 0xc0.168.0.1 (#89) and three-part 192.168.1 (#88).
    """
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("0xc0.168.1")
    assert exc.value.status_code == 403


def test_fetch_siit_rfc1918_172_16_is_blocked_before_get():
    """[::ffff:0:172.16.0.1] is reserved-only SIIT; fetch must 403 before GET."""
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
                    "https://[::ffff:0:172.16.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_nat64_rfc1918_192_168_is_blocked_before_get():
    """[64:ff9b::192.168.0.1] is reserved-only; fetch must 403 before GET."""
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
                    "https://[64:ff9b::192.168.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_three_part_mixed_rfc1918_192_168_is_blocked_before_get():
    """0xc0.168.1 is 192.168.0.1 via getaddrinfo; fetch must 403 before GET."""
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
                fetch_url_document("https://0xc0.168.1/secret", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == []
