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


def test_parse_accepts_port_zero_and_max_valid_port():
    """Port is parsed then unused for SSRF; 0 and 65535 must still parse as public hosts."""
    raw0, host0, port0 = _parse_and_validate_url("https://example.com:0/x")
    assert host0 == "example.com"
    assert port0 == 0
    assert ":0" in raw0

    raw_hi, host_hi, port_hi = _parse_and_validate_url("https://example.com:65535/x")
    assert host_hi == "example.com"
    assert port_hi == 65535
    assert ":65535" in raw_hi


def test_parse_ipv6_loopback_with_explicit_port_still_exposes_unbracketed_host():
    raw, host, port = _parse_and_validate_url("https://[::1]:8080/internal")
    assert host == "::1"
    assert port == 8080
    assert "[::1]:8080" in raw
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_classify_pdf_magic_overrides_text_plain_and_markdown():
    """Magic is checked before MIME. text/* would UTF-8-normalize and corrupt binary PDFs."""
    pdf = b"%PDF-1.4\n%\xff binary"
    assert _classify_body("text/plain; charset=utf-8", pdf) == "pdf"
    assert _classify_body("text/markdown", pdf) == "pdf"


def test_fetch_loopback_with_https_port_is_blocked_before_get():
    """SSRF uses hostname, not port; :443 on loopback must not issue HTTP."""
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
                    "https://127.0.0.1:443/secret", _url_import_settings()
                )
            )
    assert exc.value.status_code == 403
    assert requests == []


def test_fetch_redirect_userinfo_to_loopback_is_blocked_before_follow_up_get():
    """Location userinfo must not skip host checks on the next hop (classic SSRF disguise)."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "https://evil@127.0.0.1/secret"},
            ),
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            ),
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/start", _url_import_settings())
            )
    assert exc.value.status_code == 403
    assert requests == [("GET", "https://example.com/start")]


def test_fetch_redirect_userinfo_does_not_override_next_hop_hostname():
    """https://127.0.0.1@example.com/ has host example.com; the hop must GET that host, not loopback."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "https://127.0.0.1@example.com/ok"},
            ),
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"public-ok",),
            ),
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/start", _url_import_settings())
        )
    assert body == b"public-ok"
    assert ext == ".txt"
    assert requests == [
        ("GET", "https://example.com/start"),
        ("GET", "https://127.0.0.1@example.com/ok"),
    ]


def test_fetch_same_directory_relative_location_stays_on_path():
    """`./file.txt` from /docs/page.html must urljoin to /docs/file.txt, not /file.txt."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "./file.txt"},
            ),
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"sibling",),
            ),
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/docs/page.html", _url_import_settings()
            )
        )
    assert body == b"sibling"
    assert ext == ".txt"
    assert requests == [
        ("GET", "https://example.com/docs/page.html"),
        ("GET", "https://example.com/docs/file.txt"),
    ]


def test_fetch_pdf_magic_under_text_plain_keeps_raw_bytes():
    """If magic lost to MIME, UTF-8 replace would corrupt high bytes before S3/worker PDF parse."""
    raw_pdf = b"%PDF-1.4\n%\xff binary"
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain; charset=utf-8"},
                chunks=(raw_pdf,),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/doc", _url_import_settings())
        )
    assert body == raw_pdf
    assert ext == ".pdf"
    assert requests == [("GET", "https://example.com/doc")]
