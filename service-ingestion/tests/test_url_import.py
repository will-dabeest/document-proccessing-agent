import asyncio
from types import SimpleNamespace

import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _StreamResponse:
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
        assert self.status_code < 400


class _FakeAsyncClient:
    def __init__(self, responses: list[_StreamResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    def stream(self, method: str, url: str):
        self.requests.append((method, url))
        if not self._responses:
            raise AssertionError("No fake response queued")
        return self._responses.pop(0)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_async_client(
    monkeypatch: pytest.MonkeyPatch, responses: list[_StreamResponse]
) -> _FakeAsyncClient:
    client = _FakeAsyncClient(responses)
    monkeypatch.setattr(
        "app.url_import.httpx.AsyncClient",
        lambda *_args, **_kwargs: client,
    )
    return client


def test_parse_rejects_file_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_parse_rejects_empty_url():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("   ")
    assert exc.value.status_code == 400


def test_parse_rejects_overly_long_url():
    long_url = "https://example.com/" + ("a" * 2048)

    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(long_url)

    assert exc.value.status_code == 400
    assert exc.value.detail == "URL is too long"


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


@pytest.mark.parametrize("hostname", ["[::1]", "192.168.1.1"])
def test_raise_for_private_blocks_literal_private_hosts(hostname):
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)

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


def test_body_for_storage_preserves_markdown_extension():
    body, ext = _body_for_storage(
        "text",
        b"# Title\n\nBody",
        "text/markdown; charset=utf-8",
    )

    assert body == b"# Title\n\nBody"
    assert ext == ".md"


def test_body_for_storage_rejects_unknown_binary():
    with pytest.raises(UrlImportError) as exc:
        _body_for_storage("unknown_binary", b"PK\x03\x04", "application/zip")

    assert exc.value.status_code == 415
    assert "Unsupported content type" in exc.value.detail


def test_fetch_url_document_follows_relative_redirect_and_revalidates(monkeypatch):
    client = _install_async_client(
        monkeypatch,
        [
            _StreamResponse(302, headers={"location": "/final.txt"}),
            _StreamResponse(
                200,
                headers={"content-type": "text/plain; charset=utf-8"},
                chunks=[b"hello from redirect"],
            ),
        ],
    )
    checked_hosts: list[str] = []
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts",
        lambda host: checked_hosts.append(host),
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/start", _settings())
    )

    assert body == b"hello from redirect"
    assert ext == ".txt"
    assert checked_hosts == ["example.com", "example.com"]
    assert client.requests == [
        ("GET", "https://example.com/start"),
        ("GET", "https://example.com/final.txt"),
    ]


def test_fetch_url_document_rejects_redirect_without_location(monkeypatch):
    client = _install_async_client(
        monkeypatch,
        [_StreamResponse(302, headers={})],
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts",
        lambda _host: None,
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Redirect without Location header"
    assert client.requests == [("GET", "https://example.com/start")]


def test_fetch_url_document_respects_zero_redirect_limit(monkeypatch):
    client = _install_async_client(
        monkeypatch,
        [_StreamResponse(302, headers={"location": "/next"})],
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts",
        lambda _host: None,
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/start",
                _settings(url_import_max_redirects=0),
            )
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Too many redirects"
    assert client.requests == [("GET", "https://example.com/start")]
