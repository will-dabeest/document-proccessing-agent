import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(
        self,
        status_code=200,
        headers=None,
        chunks: list[bytes] | None = None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.com/failure")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("remote failure", request=request, response=response)


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method, url):
        assert method == "GET"
        self.requests.append(url)
        return self._responses.pop(0)


def _settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_client(monkeypatch, responses):
    from app import url_import

    clients: list[_FakeAsyncClient] = []

    def factory(*_args, **_kwargs):
        client = _FakeAsyncClient(responses)
        clients.append(client)
        return client

    monkeypatch.setattr(url_import.httpx, "AsyncClient", factory)
    return clients


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
    assert exc.value.detail == "Invalid port"


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


def test_fetch_url_document_revalidates_redirect_target_before_fetch(monkeypatch):
    from app import url_import

    clients = _patch_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                status_code=302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            ),
            _FakeStreamResponse(chunks=[b"secret metadata"]),
        ],
    )
    checked_hosts: list[str] = []

    def fake_validate(host):
        checked_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", fake_validate)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert checked_hosts == ["example.com", "169.254.169.254"]
    assert clients[0].requests == ["https://example.com/start"]


def test_fetch_url_document_enforces_streamed_size_limit(monkeypatch):
    from app import url_import

    _patch_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            )
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/big.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413


def test_fetch_url_document_preserves_markdown_extension(monkeypatch):
    from app import url_import

    _patch_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nbody"],
            )
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    body, ext = asyncio.run(fetch_url_document("https://example.com/doc.md", _settings()))

    assert body == b"# Title\n\nbody"
    assert ext == ".md"
