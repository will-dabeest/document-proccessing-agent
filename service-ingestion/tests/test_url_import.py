from types import SimpleNamespace

import httpx
import pytest

from app import url_import
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


def test_raise_for_private_blocks_dns_resolved_private_host(monkeypatch):
    def _private_addrinfo(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 443))]

    monkeypatch.setattr(url_import.socket, "getaddrinfo", _private_addrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("documents.example")

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


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.com/doc")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("remote error", request=request, response=response)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
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


def _install_fake_client(monkeypatch, responses):
    client = _FakeAsyncClient(responses)
    monkeypatch.setattr(url_import.httpx, "AsyncClient", lambda **_kwargs: client)
    return client


@pytest.mark.asyncio
async def test_fetch_revalidates_redirect_target_before_following(monkeypatch):
    client = _install_fake_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://127.0.0.1/latest/meta-data"},
            ),
            _FakeStreamResponse(200, headers={"content-type": "text/plain"}, chunks=[b"secret"]),
        ],
    )
    validated_hosts = []

    def _validate_host(host):
        validated_hosts.append(host)
        if host == "127.0.0.1":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", _validate_host)

    with pytest.raises(UrlImportError) as exc:
        await fetch_url_document("https://example.com/start", _settings())

    assert exc.value.status_code == 403
    assert validated_hosts == ["example.com", "127.0.0.1"]
    assert client.requests == [("GET", "https://example.com/start")]


@pytest.mark.asyncio
async def test_fetch_enforces_streamed_body_limit(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"hello", b"world"],
            ),
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        await fetch_url_document(
            "https://example.com/large.txt",
            _settings(url_import_max_bytes=7),
        )

    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_fetch_maps_remote_http_status_to_bad_gateway(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeStreamResponse(404)])
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        await fetch_url_document("https://example.com/missing", _settings())

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


@pytest.mark.asyncio
async def test_fetch_preserves_markdown_extension(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nBody"],
            ),
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    body, ext = await fetch_url_document("https://example.com/readme.md", _settings())

    assert body == b"# Title\n\nBody"
    assert ext == ".md"


@pytest.mark.asyncio
async def test_fetch_stops_after_configured_redirect_limit(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                302,
                headers={"location": "https://example.com/next"},
            ),
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        await fetch_url_document(
            "https://example.com/start",
            _settings(url_import_max_redirects=0),
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Too many redirects"
