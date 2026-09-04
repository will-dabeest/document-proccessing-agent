import asyncio
import socket
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


def _public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


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


def test_parse_accepts_mixed_case_scheme_and_host():
    """Scheme is lowercased for the allow-list; hostname is lowercased for SSRF; raw keeps caller casing."""
    raw, host, port = _parse_and_validate_url("HTTP://Example.COM/Path")
    assert host == "example.com"
    assert port is None
    assert raw.startswith("HTTP://")
    assert "Example.COM" in raw


@pytest.mark.parametrize(
    "content_type",
    ["text/css", "application/javascript", "text/javascript"],
)
def test_classify_stylesheet_and_script_are_unknown_binary(content_type):
    """Allow-list is HTML/PDF/text/markdown only; CSS/JS must not be stored as documents."""
    assert _classify_body(content_type, b"body { color: red; }") == "unknown_binary"


def test_raise_for_private_blocks_dotted_quad_with_leading_zeros():
    """ipaddress rejects 127.0.0.01; glibc getaddrinfo still maps it to loopback."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.01")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_ipv4_compatible_ipv6_loopback():
    """Deprecated ::127.0.0.1 is not IPv4-mapped (::ffff:...); it is still reserved/disallowed."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("::127.0.0.1")
    assert exc.value.status_code == 403


def test_fetch_leading_zero_loopback_is_blocked_before_get():
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
                    "https://127.0.0.01/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_ipv4_compatible_loopback_is_blocked_before_get():
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
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=AssertionError("dns")
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://[::127.0.0.1]/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_fragment_only_redirect_keeps_path():
    """`#section` must urljoin onto the current path, not be treated as a new hop off-site."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "#section"},
            ),
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"same-page",),
            ),
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/docs/page", _url_import_settings()
            )
        )
    assert body == b"same-page"
    assert ext == ".txt"
    assert requests == [
        ("GET", "https://example.com/docs/page"),
        ("GET", "https://example.com/docs/page#section"),
    ]


def test_fetch_rejects_css_content_type():
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/css"},
                chunks=(b"h1 { font-size: 2rem; }",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/app.css", _url_import_settings())
            )
    assert exc.value.status_code == 415
    assert requests == [("GET", "https://example.com/app.css")]
