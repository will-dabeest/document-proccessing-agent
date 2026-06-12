import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.url_import as url_import
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
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.request = httpx.Request("GET", "https://example.com/")

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
                f"HTTP {self.status_code}", request=self.request, response=response
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


def _allow_example_dns(monkeypatch):
    monkeypatch.setattr(
        url_import.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, "", ("93.184.216.34", 0))],
    )


def _install_fake_async_client(monkeypatch, responses: list[_FakeStreamResponse]):
    requested_urls: list[str] = []

    class _FakeAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return False

        def stream(self, method: str, url: str):
            assert method == "GET"
            requested_urls.append(url)
            if not responses:
                raise AssertionError(f"unexpected request for {url}")
            response = responses.pop(0)
            response.request = httpx.Request(method, url)
            return response

    monkeypatch.setattr(url_import.httpx, "AsyncClient", _FakeAsyncClient)
    return requested_urls


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


def test_fetch_url_document_follows_relative_redirect_and_keeps_markdown_ext(
    monkeypatch,
):
    _allow_example_dns(monkeypatch)
    requested = _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(302, headers={"location": "/docs/readme.md"}),
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nbody"],
            ),
        ],
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/start", _settings())
    )

    assert requested == [
        "https://example.com/start",
        "https://example.com/docs/readme.md",
    ]
    assert body == b"# Title\n\nbody"
    assert ext == ".md"


def test_fetch_url_document_revalidates_redirect_target_before_fetch(monkeypatch):
    _allow_example_dns(monkeypatch)
    requested = _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://127.0.0.1/latest/meta-data"},
            ),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert requested == ["https://example.com/start"]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    _allow_example_dns(monkeypatch)
    _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"1234", b"56"],
            ),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413


def test_fetch_url_document_maps_remote_http_errors(monkeypatch):
    _allow_example_dns(monkeypatch)
    _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                404,
                headers={"content-type": "text/plain"},
                chunks=[b"missing"],
            ),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing.txt", _settings()))

    assert exc.value.status_code == 502
    assert "404" in exc.value.detail
