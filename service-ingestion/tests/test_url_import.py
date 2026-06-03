import asyncio
from types import SimpleNamespace

import pytest

import app.url_import as url_import
from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


class _FakeResponse:
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
        self.url = "https://example.com/"

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = url_import.httpx.Request("GET", self.url)
        response = url_import.httpx.Response(self.status_code, request=request)
        raise url_import.httpx.HTTPStatusError(
            f"HTTP {self.status_code}", request=request, response=response
        )


class _FakeStream:
    def __init__(self, response: _FakeResponse, url: str) -> None:
        self._response = response
        self._url = url

    async def __aenter__(self) -> _FakeResponse:
        self._response.url = self._url
        return self._response

    async def __aexit__(self, *_exc_info) -> None:
        return None


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_async_client(monkeypatch, responses: list[_FakeResponse]) -> list[str]:
    requests: list[str] = []
    queued = list(responses)

    class _FakeAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info) -> None:
            return None

        def stream(self, method: str, url: str):
            assert method == "GET"
            requests.append(url)
            if not queued:
                raise AssertionError(f"unexpected request to {url}")
            return _FakeStream(queued.pop(0), url)

    monkeypatch.setattr(url_import.httpx, "AsyncClient", _FakeAsyncClient)
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


def test_parse_rejects_invalid_port_as_bad_request():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:bad/path")

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
    requests = _patch_async_client(
        monkeypatch,
        [
            _FakeResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ],
    )
    validated_hosts: list[str] = []

    def fake_validate(host: str) -> None:
        validated_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", fake_validate)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document("https://example.com/start", _settings())
        )

    assert exc.value.status_code == 403
    assert validated_hosts == ["example.com", "169.254.169.254"]
    assert requests == ["https://example.com/start"]


def test_fetch_url_document_enforces_streamed_size_limit(monkeypatch):
    requests = _patch_async_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            )
        ],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/large.txt", _settings(url_import_max_bytes=5)
            )
        )

    assert exc.value.status_code == 413
    assert requests == ["https://example.com/large.txt"]


def test_fetch_url_document_maps_remote_http_status(monkeypatch):
    _patch_async_client(
        monkeypatch,
        [_FakeResponse(404, headers={"content-type": "text/plain"}, chunks=[b"missing"])],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document("https://example.com/missing", _settings())
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_url_document_preserves_markdown_extension(monkeypatch):
    body = b"# Release notes\n\nRegression coverage."
    requests = _patch_async_client(
        monkeypatch,
        [_FakeResponse(200, headers={"content-type": "text/markdown"}, chunks=[body])],
    )
    monkeypatch.setattr(url_import, "raise_for_private_or_meta_hosts", lambda _host: None)

    stored_body, ext = asyncio.run(
        url_import.fetch_url_document("https://example.com/notes.md", _settings())
    )

    assert stored_body == body
    assert ext == ".md"
    assert requests == ["https://example.com/notes.md"]
