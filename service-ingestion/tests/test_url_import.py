import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _ip_from_sockaddr,
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
        "url_import_max_redirects": 2,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _response(*, status_code=200, headers=None, chunks=()):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.aread = AsyncMock(return_value=b"")
    response.raise_for_status = MagicMock()

    async def _iter_bytes():
        for chunk in chunks:
            yield chunk

    response.aiter_bytes = _iter_bytes
    return response


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    @asynccontextmanager
    async def stream(self, method, url):
        assert method == "GET"
        self.requested_urls.append(url)
        yield next(self.responses)


def _loopback_dns_result(*_args, **_kwargs):
    return [(2, 1, 6, "", ("127.0.0.1", 0))]


def _public_dns_result(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_ip_from_sockaddr_accepts_ipv4_and_ipv6_tuples():
    assert _ip_from_sockaddr(("93.184.216.34", 0)) == "93.184.216.34"
    assert _ip_from_sockaddr(("2001:db8::1", 0, 0, 0)) == "2001:db8::1"


def test_ip_from_sockaddr_skips_unusable_records():
    assert _ip_from_sockaddr(None) is None
    assert _ip_from_sockaddr(()) is None
    assert _ip_from_sockaddr((12345,)) is None
    assert _ip_from_sockaddr("93.184.216.34") is None


def test_parse_rejects_data_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("data:text/plain,ssrf-payload")
    assert exc.value.status_code == 400
    assert "http" in exc.value.detail.lower()


@pytest.mark.parametrize(
    "url, host",
    [
        ("http://127.1/internal", "127.1"),
        ("http://127.0.1/internal", "127.0.1"),
        ("http://2130706433/internal", "2130706433"),
        ("http://0x7f000001/internal", "0x7f000001"),
        ("http://0177.0.0.1/internal", "0177.0.0.1"),
    ],
)
def test_parse_accepts_encoded_numeric_hosts(url, host):
    """ipaddress rejects these forms; parse must still expose the host for DNS SSRF checks."""
    raw, parsed_host, port = _parse_and_validate_url(url)
    assert parsed_host == host
    assert port is None
    assert host in raw


@pytest.mark.parametrize(
    "hostname",
    [
        "127.1",
        "127.0.1",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
    ],
)
def test_raise_for_private_blocks_encoded_loopback_via_dns(hostname):
    """Numeric hosts that fail ipaddress still resolve through getaddrinfo (glibc inet_aton)."""
    with patch("app.url_import.socket.getaddrinfo", side_effect=_loopback_dns_result) as gai:
        with pytest.raises(UrlImportError) as exc:
            raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()
    gai.assert_called_once()


@pytest.mark.parametrize(
    "hostname",
    [
        "fe80::1%lo",
        "[fe80::1%lo]",
        "fe80::1%25lo",
    ],
)
def test_raise_for_private_blocks_ipv6_zone_id_link_local(hostname):
    """Scoped link-local literals must not skip SSRF checks because of the zone suffix."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_fetch_rejects_abbreviated_loopback_before_get():
    client = _FakeAsyncClient(
        [_response(headers={"content-type": "text/plain"}, chunks=(b"secret",))]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_loopback_dns_result
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("http://127.1/secret", _url_import_settings())
            )

    assert exc.value.status_code == 403
    assert client.requested_urls == []


@pytest.mark.parametrize("status_code", [300, 304, 305, 306])
def test_fetch_does_not_follow_non_redirect_3xx_location(status_code):
    """Only 301/302/303/307/308 hop; other 3xx plus Location must not be used as SSRF redirects."""
    client = _FakeAsyncClient(
        [
            _response(
                status_code=status_code,
                headers={"location": "http://127.0.0.1/secret"},
            ),
            _response(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/start",
                    _url_import_settings(),
                )
            )

    assert exc.value.status_code == 415
    assert client.requested_urls == ["https://example.com/start"]
