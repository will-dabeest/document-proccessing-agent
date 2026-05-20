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
        *,
        status_code: int,
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
            request = httpx.Request("GET", "https://example.com/doc")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "remote error",
                request=request,
                response=response,
            )


class _FakeAsyncClient:
    instances: list["_FakeAsyncClient"] = []
    responses: list[_FakeStreamResponse] = []

    def __init__(self, **_kwargs: object) -> None:
        self.requests: list[tuple[str, str]] = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    def stream(self, method: str, url: str):
        self.requests.append((method, url))
        return type(self).responses.pop(0)


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
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


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:99999/path",
        "https://example.com:not-a-port/path",
    ],
)
def test_parse_rejects_invalid_port_as_url_import_error(url):
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(url)

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


def test_fetch_url_document_revalidates_redirect_targets(monkeypatch):
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.responses = [
        _FakeStreamResponse(
            status_code=302,
            headers={"location": "http://169.254.169.254/latest"},
        )
    ]
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        "app.url_import.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert _FakeAsyncClient.instances[0].requests == [
        ("GET", "https://example.com/start"),
    ]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.responses = [
        _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            chunks=[b"abc", b"def"],
        )
    ]
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        "app.url_import.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
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
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.responses = [
        _FakeStreamResponse(status_code=503, headers={"content-type": "text/plain"})
    ]
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        "app.url_import.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/unavailable", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 503"
