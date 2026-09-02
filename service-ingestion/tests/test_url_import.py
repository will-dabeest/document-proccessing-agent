import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
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
        # Mirrors httpx: only 4xx/5xx raise. 2xx and 3xx succeed.
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


def test_classify_markdown_vendor_subtype_via_substring():
    """'markdown' in the MIME type is enough; it need not be text/markdown or text/x-markdown."""
    assert _classify_body("application/markdown", b"# Title") == "text"
    assert _classify_body("application/vnd.github.markdown", b"# Title") == "text"


def test_body_for_storage_application_markdown_uses_md_extension():
    body, ext = _body_for_storage("text", b"# Title\n", "application/markdown")
    assert body == b"# Title\n"
    assert ext == ".md"


def test_classify_pdf_substring_outside_application_pdf_types():
    """The 'pdf' substring catch-all is not limited to application/pdf and application/x-pdf."""
    assert _classify_body("text/pdf", b"not-magic") == "pdf"
    assert _classify_body("application/acrobat-pdf", b"not-magic") == "pdf"


@pytest.mark.parametrize("status_code", [300, 304, 305, 306])
def test_fetch_non_redirect_3xx_with_body_is_stored_and_location_is_ignored(status_code):
    """Only 301/302/303/307/308 hop. Other 3xx with a body are stored; Location is not fetched."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                status_code=status_code,
                headers={
                    "content-type": "text/plain",
                    "location": "http://127.0.0.1/secret",
                },
                chunks=(b"choose-this",),
            ),
            _FakeStreamResponse(
                status_code=200,
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            ),
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/negotiate", _url_import_settings())
        )

    assert body == b"choose-this"
    assert ext == ".txt"
    assert requests == [("GET", "https://example.com/negotiate")]
