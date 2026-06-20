import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.url_import as url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
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
            request = httpx.Request("GET", "https://example.com/document.txt")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("remote error", request=request, response=response)


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeStreamResponse], requests: list[str]) -> None:
        self._responses = responses
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    def stream(self, method: str, url: str):
        assert method == "GET"
        self._requests.append(url)
        return self._responses.pop(0)


def _fetch_settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 2,
        "url_import_max_bytes": 32,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_async_client(monkeypatch, responses: list[_FakeStreamResponse]) -> list[str]:
    requests: list[str] = []
    monkeypatch.setattr(
        url_import.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(responses, requests),
    )
    return requests


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


def test_fetch_revalidates_redirect_target_before_second_request(monkeypatch):
    requests = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ],
    )

    def fake_validate(host: str) -> None:
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", fake_validate)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/start",
                _fetch_settings(),
            )
        )

    assert exc.value.status_code == 403
    assert requests == ["https://example.com/start"]


def test_fetch_enforces_streamed_body_limit(monkeypatch):
    requests = _patch_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"0123456789", b"over-limit"],
            )
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/large.txt",
                _fetch_settings(url_import_max_bytes=10),
            )
        )

    assert exc.value.status_code == 413
    assert requests == ["https://example.com/large.txt"]


def test_fetch_maps_remote_http_errors_to_bad_gateway(monkeypatch):
    _patch_async_client(
        monkeypatch,
        [_FakeStreamResponse(404, headers={"content-type": "text/plain"})],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/missing.txt",
                _fetch_settings(),
            )
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"
