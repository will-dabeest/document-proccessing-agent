import asyncio
import socket
from types import SimpleNamespace

import pytest

from app import url_import

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
            request = url_import.httpx.Request("GET", "https://example.com")
            response = url_import.httpx.Response(self.status_code, request=request)
            raise url_import.httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=response,
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


def _install_fake_async_client(monkeypatch, responses: list[_FakeStreamResponse]):
    calls: list[tuple[str, str]] = []
    response_iter = iter(responses)

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return False

        def stream(self, method: str, url: str):
            calls.append((method, url))
            try:
                return next(response_iter)
            except StopIteration as e:
                raise AssertionError(f"unexpected request to {url}") from e

    monkeypatch.setattr(url_import.httpx, "AsyncClient", FakeAsyncClient)
    return calls


def _resolve_example_to_public_ip(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host != "example.com":
            raise socket.gaierror(f"unexpected host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(url_import.socket, "getaddrinfo", fake_getaddrinfo)


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
    "bad_url",
    [
        "https://example.com:not-a-port/path",
        "https://example.com:99999/path",
    ],
)
def test_parse_rejects_invalid_port_as_bad_request(bad_url):
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(bad_url)
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
    _resolve_example_to_public_ip(monkeypatch)
    calls = _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert calls == [("GET", "https://example.com/start")]


def test_fetch_enforces_streamed_response_byte_limit(monkeypatch):
    _resolve_example_to_public_ip(monkeypatch)
    calls = _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"abcd", b"ef"],
            )
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
    assert calls == [("GET", "https://example.com/large.txt")]


def test_fetch_preserves_markdown_extension(monkeypatch):
    _resolve_example_to_public_ip(monkeypatch)
    _install_fake_async_client(
        monkeypatch,
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Release notes\n"],
            )
        ],
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/release-notes", _settings())
    )

    assert body == b"# Release notes\n"
    assert ext == ".md"
