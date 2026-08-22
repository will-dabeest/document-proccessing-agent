import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
        "url_import_max_redirects": 2,
        "url_import_timeout_seconds": 9.25,
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


def _public_dns_result(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_parse_accepts_http_scheme():
    raw, host, port = _parse_and_validate_url("http://example.com/a")
    assert host == "example.com"
    assert raw.startswith("http://")
    assert port is None


def test_parse_preserves_query_string_in_raw_url():
    raw, host, _port = _parse_and_validate_url(
        "https://example.com/doc.txt?download=1&token=abc"
    )
    assert host == "example.com"
    assert "download=1" in raw
    assert "token=abc" in raw


@pytest.mark.parametrize(
    "hostname",
    [
        "172.16.0.1",  # RFC1918 172.16/12 (open PRs cover 10/8 and 192.168/16)
        "192.0.2.1",  # TEST-NET-1 documentation
        "198.51.100.1",  # TEST-NET-2
        "203.0.113.1",  # TEST-NET-3
        "198.18.0.1",  # RFC 2544 benchmarking
        "2001:db8::1",  # IPv6 documentation
        "[2001:db8::1]",
    ],
)
def test_raise_for_private_blocks_documentation_and_rfc1918b_literals(hostname):
    """IETF special-use / documentation ranges must fail closed like RFC1918."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_raise_for_private_strips_surrounding_whitespace_on_literal():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("  172.16.0.1  ")
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "mapped",
    [
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
    ],
)
def test_raise_for_private_blocks_ipv4_mapped_private_from_dns(mapped):
    """getaddrinfo IPv4-mapped forms are not URL literals; is_loopback is false for ::ffff:127.0.0.1."""

    def _mapped_dns(*_args, **_kwargs):
        return [(10, 1, 6, "", (mapped, 0, 0, 0))]

    with patch("app.url_import.socket.getaddrinfo", side_effect=_mapped_dns) as gai:
        with pytest.raises(UrlImportError) as exc:
            raise_for_private_or_meta_hosts("mapped.example")
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()
    gai.assert_called_once()


def test_fetch_rejects_ipv4_mapped_loopback_dns_before_get():
    client = _FakeAsyncClient(
        [_response(headers={"content-type": "text/plain"}, chunks=(b"secret",))]
    )

    def _mapped_loopback_dns(*_args, **_kwargs):
        return [(10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))]

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_mapped_loopback_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://mapped.example/secret",
                    _url_import_settings(),
                )
            )

    assert exc.value.status_code == 403
    assert client.requested_urls == []


def test_fetch_rejects_dns_rebinding_on_redirect_hop():
    """Same hostname may resolve public then private; hop 2 must 403 before the second GET."""
    client = _FakeAsyncClient(
        [
            _response(
                status_code=302,
                headers={"location": "https://example.com/next"},
            ),
            _response(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            ),
        ]
    )
    resolutions = iter(
        [
            [(2, 1, 6, "", ("93.184.216.34", 0))],
            [(2, 1, 6, "", ("127.0.0.1", 0))],
        ]
    )

    def _rebinding_dns(*_args, **_kwargs):
        return next(resolutions)

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_rebinding_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/start",
                    _url_import_settings(),
                )
            )

    assert exc.value.status_code == 403
    assert client.requested_urls == ["https://example.com/start"]


def test_fetch_preserves_query_string_on_get():
    client = _FakeAsyncClient(
        [
            _response(
                headers={"content-type": "text/plain; charset=utf-8"},
                chunks=(b"ok",),
            )
        ]
    )
    url = "https://example.com/doc.txt?download=1&token=abc"
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ):
        body, ext = asyncio.run(fetch_url_document(url, _url_import_settings()))

    assert body == b"ok"
    assert ext == ".txt"
    assert client.requested_urls == [url]


def test_fetch_passes_configured_timeout_to_httpx_client():
    client = _FakeAsyncClient(
        [
            _response(
                headers={"content-type": "text/plain"},
                chunks=(b"ok",),
            )
        ]
    )
    captured = {}

    def _factory(*_args, **kwargs):
        captured.update(kwargs)
        return client

    with patch("app.url_import.httpx.AsyncClient", side_effect=_factory), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ):
        asyncio.run(
            fetch_url_document(
                "https://example.com/a.txt",
                _url_import_settings(url_import_timeout_seconds=9.25),
            )
        )

    timeout = captured["timeout"]
    assert timeout.read == 9.25
    assert timeout.connect == 9.25
