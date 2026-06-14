import asyncio
from types import SimpleNamespace

import pytest

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

    async def __aexit__(self, *_exc):
        return None

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeStreamResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def stream(self, method: str, url: str):
        self.requests.append((method, url))
        return self._responses.pop(0)


def _url_import_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 30.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns(*_args, **_kwargs):
    return [(None, None, None, None, ("93.184.216.34", 0))]


def test_parse_rejects_file_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_parse_rejects_empty_url():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("   ")
    assert exc.value.status_code == 400


def test_parse_rejects_invalid_port():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:notaport/path")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid port"


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


def test_fetch_revalidates_redirect_target_before_second_request(monkeypatch):
    import app.url_import as url_import

    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ]
    )
    monkeypatch.setattr(url_import.httpx, "AsyncClient", lambda *_a, **_kw: client)
    monkeypatch.setattr(url_import.socket, "getaddrinfo", _public_dns)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/start",
                _url_import_settings(),
            )
        )

    assert exc.value.status_code == 403
    assert client.requests == [("GET", "https://example.com/start")]


def test_fetch_enforces_streamed_response_limit(monkeypatch):
    import app.url_import as url_import

    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"1234", b"56"],
            )
        ]
    )
    monkeypatch.setattr(url_import.httpx, "AsyncClient", lambda *_a, **_kw: client)
    monkeypatch.setattr(url_import.socket, "getaddrinfo", _public_dns)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            url_import.fetch_url_document(
                "https://example.com/doc.txt",
                _url_import_settings(url_import_max_bytes=5),
            )
        )

    assert exc.value.status_code == 413
    assert client.requests == [("GET", "https://example.com/doc.txt")]


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out
