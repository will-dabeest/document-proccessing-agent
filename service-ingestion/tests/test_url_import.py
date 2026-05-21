import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.url_import as url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
    fetch_url_document,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.request = httpx.Request("GET", "https://example.com/test")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=self.request, response=response
            )


def _fake_async_client_factory(
    responses: list[_FakeStreamResponse],
    seen_requests: list[tuple[str, str]],
):
    class _FakeAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self._responses = list(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return False

        def stream(self, method: str, url: str):
            seen_requests.append((method, url))
            if not self._responses:
                raise AssertionError(f"unexpected request for {url}")
            return self._responses.pop(0)

    return _FakeAsyncClient


def _settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 5,
        "url_import_max_redirects": 3,
        "url_import_max_bytes": 1024,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


def test_parse_rejects_invalid_port_as_bad_request():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:not-a-port/path")
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


def test_fetch_url_document_revalidates_redirect_target_before_request(monkeypatch):
    seen_requests: list[tuple[str, str]] = []
    validated_hosts: list[str] = []
    responses = [
        _FakeStreamResponse(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        )
    ]

    def fake_validate(host: str) -> None:
        validated_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", fake_validate)
    monkeypatch.setattr(
        url_import.httpx,
        "AsyncClient",
        _fake_async_client_factory(responses, seen_requests),
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert validated_hosts == ["example.com", "169.254.169.254"]
    assert seen_requests == [("GET", "https://example.com/start")]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    seen_requests: list[tuple[str, str]] = []
    responses = [
        _FakeStreamResponse(
            200,
            headers={"content-type": "text/plain"},
            chunks=[b"abc", b"def"],
        )
    ]

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)
    monkeypatch.setattr(
        url_import.httpx,
        "AsyncClient",
        _fake_async_client_factory(responses, seen_requests),
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert seen_requests == [("GET", "https://example.com/large.txt")]


def test_fetch_url_document_maps_remote_http_errors(monkeypatch):
    seen_requests: list[tuple[str, str]] = []
    responses = [
        _FakeStreamResponse(
            503,
            headers={"content-type": "text/plain"},
            chunks=[b"temporarily unavailable"],
        )
    ]

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)
    monkeypatch.setattr(
        url_import.httpx,
        "AsyncClient",
        _fake_async_client_factory(responses, seen_requests),
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/down", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 503"
    assert seen_requests == [("GET", "https://example.com/down")]


def test_fetch_url_document_preserves_markdown_extension(monkeypatch):
    seen_requests: list[tuple[str, str]] = []
    responses = [
        _FakeStreamResponse(
            200,
            headers={"content-type": "text/markdown; charset=utf-8"},
            chunks=["Snowman: \u2603".encode()],
        )
    ]

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)
    monkeypatch.setattr(
        url_import.httpx,
        "AsyncClient",
        _fake_async_client_factory(responses, seen_requests),
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/readme.md", _settings())
    )

    assert body == "Snowman: \u2603".encode()
    assert ext == ".md"
    assert seen_requests == [("GET", "https://example.com/readme.md")]
