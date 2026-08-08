import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
    _classify_body,
    _html_to_plain_text,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


def _url_import_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 2,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _response(*, status_code=200, headers=None, chunks=()):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.aread = AsyncMock(return_value=b"")
    response.raise_for_status = MagicMock()

    async def _iter_bytes():
        for chunk in chunks:
            yield chunk

    response.aiter_bytes = _iter_bytes
    return response


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    @asynccontextmanager
    async def stream(self, method, url):
        assert method == "GET"
        self.requested_urls.append(url)
        yield next(self.responses)


def _public_dns_result(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


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
    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


@pytest.mark.parametrize("status_code", [301, 303, 307, 308])
def test_fetch_follows_non_302_redirects_and_revalidates_host(status_code):
    """Open coverage historically locked 302; other redirect codes must still revalidate."""
    client = _FakeAsyncClient(
        [
            _response(
                status_code=status_code,
                headers={"location": "https://example.com/final"},
            ),
            _response(
                headers={"content-type": "text/plain"},
                chunks=(b"redirected body",),
            ),
        ]
    )
    hosts_checked: list[str] = []

    def _track_host(hostname: str) -> None:
        hosts_checked.append(hostname)
        raise_for_private_or_meta_hosts(hostname)

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ), patch(
        "app.url_import.raise_for_private_or_meta_hosts", side_effect=_track_host
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/start",
                _url_import_settings(),
            )
        )

    assert body == b"redirected body"
    assert ext == ".txt"
    assert client.requested_urls == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert hosts_checked == ["example.com", "example.com"]


def test_html_both_trafilatura_passes_empty_stores_empty_txt():
    """Silent empty HTML extraction still yields .txt bytes for S3 (no false binary)."""
    with patch(
        "app.url_import.trafilatura.extract",
        side_effect=[None, None],
    ) as extract:
        body, ext = _body_for_storage("html", b"<html><body></body></html>", "text/html")

    assert body == b""
    assert ext == ".txt"
    assert extract.call_args_list == [
        call("<html><body></body></html>", output_format="txt"),
        call("<html><body></body></html>", output_format="txt", favor_recall=True),
    ]


def test_body_for_storage_text_x_markdown_uses_md_extension():
    body, ext = _body_for_storage(
        "text",
        b"# Title\n",
        "text/x-markdown; charset=utf-8",
    )
    assert body == b"# Title\n"
    assert ext == ".md"


def test_fetch_sets_ingestion_user_agent():
    client = _FakeAsyncClient(
        [
            _response(
                headers={"content-type": "text/plain"},
                chunks=(b"ok",),
            )
        ]
    )
    captured: dict = {}

    def _factory(*_args, **kwargs):
        captured.update(kwargs)
        return client

    with patch("app.url_import.httpx.AsyncClient", side_effect=_factory), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ):
        asyncio.run(
            fetch_url_document(
                "https://example.com/ua",
                _url_import_settings(),
            )
        )

    assert captured["headers"]["User-Agent"] == "document-processing-ingestion/1.0"
    assert captured["follow_redirects"] is False
