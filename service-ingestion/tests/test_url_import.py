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
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.url = "https://example.com"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("remote error", request=request, response=response)


def _fake_async_client_factory(
    responses: list[_FakeStreamResponse],
    requests: list[str],
    init_kwargs: dict[str, object] | None = None,
):
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            init_kwargs.clear()
            init_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method: str, url: str):
            assert method == "GET"
            requests.append(url)
            response = responses.pop(0)
            response.url = url
            return response

    init_kwargs = init_kwargs if init_kwargs is not None else {}
    return _FakeAsyncClient


def _url_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 3,
        "url_import_timeout_seconds": 2.5,
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
        _parse_and_validate_url("https://example.com:notaport/doc")
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
    responses = [
        _FakeStreamResponse(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        ),
        _FakeStreamResponse(200, headers={"content-type": "text/plain"}, chunks=[b"secret"]),
    ]
    requests: list[str] = []
    seen_hosts: list[str] = []

    def reject_metadata_host(host: str) -> None:
        seen_hosts.append(host)
        if host == "169.254.169.254":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient",
        _fake_async_client_factory(responses, requests),
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", reject_metadata_host
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document("https://safe.example/start", _url_settings())
        )

    assert exc.value.status_code == 403
    assert seen_hosts == ["safe.example", "169.254.169.254"]
    assert requests == ["https://safe.example/start"]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    responses = [
        _FakeStreamResponse(
            200,
            headers={"content-type": "text/plain"},
            chunks=[b"abc", b"def"],
        )
    ]
    requests: list[str] = []
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient",
        _fake_async_client_factory(responses, requests),
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _url_settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert requests == ["https://example.com/large.txt"]


def test_fetch_url_document_maps_remote_http_errors(monkeypatch):
    responses = [_FakeStreamResponse(404, headers={"content-type": "text/plain"})]
    requests: list[str] = []
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient",
        _fake_async_client_factory(responses, requests),
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing", _url_settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_url_document_preserves_markdown_extension(monkeypatch):
    responses = [
        _FakeStreamResponse(
            200,
            headers={"content-type": "text/markdown; charset=utf-8"},
            chunks=[b"# Title\n\nBody"],
        )
    ]
    requests: list[str] = []
    init_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient",
        _fake_async_client_factory(responses, requests, init_kwargs),
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts", lambda _host: None
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/readme", _url_settings())
    )

    assert body == b"# Title\n\nBody"
    assert ext == ".md"
    assert requests == ["https://example.com/readme"]
    assert init_kwargs["follow_redirects"] is False
