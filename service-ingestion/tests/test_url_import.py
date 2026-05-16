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


class _StreamResponse:
    def __init__(
        self,
        url: str,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.request = httpx.Request("GET", url)
        self._response = httpx.Response(status_code, request=self.request)

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
            raise httpx.HTTPStatusError(
                "remote error", request=self.request, response=self._response
            )


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_async_client(monkeypatch, responses: list[_StreamResponse]) -> None:
    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self._responses = responses

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return False

        def stream(self, method: str, url: str):
            assert method == "GET"
            response = self._responses.pop(0)
            assert response.url == url
            return response

    monkeypatch.setattr("app.url_import.httpx.AsyncClient", FakeAsyncClient)


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


def test_fetch_url_revalidates_redirect_target_before_following(monkeypatch):
    responses = [
        _StreamResponse(
            "https://example.com/start",
            302,
            headers={"location": "http://127.0.0.1/latest/meta-data"},
        )
    ]
    _patch_async_client(monkeypatch, responses)
    checked_hosts: list[str] = []

    def fake_host_guard(hostname: str) -> None:
        checked_hosts.append(hostname)
        if hostname == "127.0.0.1":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", fake_host_guard
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert checked_hosts == ["example.com", "127.0.0.1"]
    assert responses == []


def test_fetch_url_enforces_streamed_response_size_limit(monkeypatch):
    responses = [
        _StreamResponse(
            "https://example.com/large.txt",
            200,
            headers={"content-type": "text/plain"},
            chunks=[b"123", b"456"],
        )
    ]
    _patch_async_client(monkeypatch, responses)
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert "limit" in exc.value.detail.lower()


def test_fetch_url_maps_remote_http_errors_to_bad_gateway(monkeypatch):
    responses = [
        _StreamResponse(
            "https://example.com/missing.txt",
            404,
            headers={"content-type": "text/plain"},
            chunks=[b"not found"],
        )
    ]
    _patch_async_client(monkeypatch, responses)
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing.txt", _settings()))

    assert exc.value.status_code == 502
    assert "404" in exc.value.detail
