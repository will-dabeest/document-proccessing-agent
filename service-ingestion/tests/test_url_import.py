import asyncio
from types import SimpleNamespace

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        return self.responses.pop(0)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


def test_parse_rejects_invalid_port():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:99999/path")
    assert exc.value.status_code == 400
    assert "port" in exc.value.detail.lower()


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


def test_fetch_revalidates_redirect_target_before_followup(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ]
    )
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient", lambda *_args, **_kwargs: fake_client
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert fake_client.requests == [("GET", "https://example.com/start")]


def test_fetch_enforces_streamed_body_limit(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            )
        ]
    )
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient", lambda *_args, **_kwargs: fake_client
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413


def test_fetch_preserves_markdown_extension(monkeypatch):
    fake_client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nBody"],
            )
        ]
    )
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient", lambda *_args, **_kwargs: fake_client
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/readme.md", _settings())
    )

    assert body == b"# Title\n\nBody"
    assert ext == ".md"
