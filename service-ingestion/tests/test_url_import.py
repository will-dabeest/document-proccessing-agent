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


class _FakeResponse:
    def __init__(
        self,
        url: str,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or [b""]

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", self.url)
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("remote error", request=request, response=response)


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_exc_info) -> None:
        return None


def _fake_async_client(routes: dict[str, _FakeResponse], requests: list[tuple[str, str]]):
    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

        def stream(self, method: str, url: str) -> _FakeStream:
            requests.append((method, url))
            return _FakeStream(routes[url])

    return FakeAsyncClient


def _settings(**overrides):
    base = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


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


def test_parse_rejects_invalid_port_as_url_import_error():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:99999/doc")
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


def test_fetch_revalidates_redirect_target_before_following(monkeypatch):
    import app.url_import as url_import

    start_url = "https://safe.example/start"
    blocked_url = "http://169.254.169.254/latest/meta-data"
    routes = {
        start_url: _FakeResponse(
            start_url,
            status_code=302,
            headers={"location": blocked_url},
        )
    }
    requests: list[tuple[str, str]] = []
    validated_hosts: list[str] = []

    def guard(host: str) -> None:
        validated_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", guard)
    monkeypatch.setattr(url_import.httpx, "AsyncClient", _fake_async_client(routes, requests))

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document(start_url, _settings()))

    assert exc.value.status_code == 403
    assert requests == [("GET", start_url)]
    assert validated_hosts == ["safe.example", "169.254.169.254"]


def test_fetch_enforces_streamed_response_size_limit(monkeypatch):
    import app.url_import as url_import

    url = "https://example.com/large.txt"
    routes = {
        url: _FakeResponse(
            url,
            headers={"content-type": "text/plain"},
            chunks=[b"abc", b"def"],
        )
    }
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)
    monkeypatch.setattr(url_import.httpx, "AsyncClient", _fake_async_client(routes, []))

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document(url, _settings(url_import_max_bytes=5)))

    assert exc.value.status_code == 413
    assert exc.value.detail == "Response body exceeds configured limit"


def test_fetch_maps_remote_http_status_to_url_import_error(monkeypatch):
    import app.url_import as url_import

    url = "https://example.com/missing"
    routes = {url: _FakeResponse(url, status_code=404)}
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)
    monkeypatch.setattr(url_import.httpx, "AsyncClient", _fake_async_client(routes, []))

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document(url, _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_preserves_markdown_extension_for_storage(monkeypatch):
    import app.url_import as url_import

    url = "https://example.com/readme"
    routes = {
        url: _FakeResponse(
            url,
            headers={"content-type": "text/markdown; charset=utf-8"},
            chunks=[b"# Title\n\nbody"],
        )
    }
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)
    monkeypatch.setattr(url_import.httpx, "AsyncClient", _fake_async_client(routes, []))

    body, ext = asyncio.run(fetch_url_document(url, _settings()))

    assert body == b"# Title\n\nbody"
    assert ext == ".md"
