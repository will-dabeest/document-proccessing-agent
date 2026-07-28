import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


def test_parse_rejects_file_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_parse_rejects_empty_url():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("   ")
    assert exc.value.status_code == 400


def test_parse_rejects_url_without_host():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https:///path-only")
    assert exc.value.status_code == 400
    assert exc.value.detail == "URL must include a host"


def test_parse_rejects_url_that_is_too_long():
    long_url = "https://example.com/" + ("a" * 2100)
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


def test_raise_for_private_rejects_invalid_idna_hostname():
    oversized_label = "a" * 64 + ".example.com"
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(oversized_label)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid hostname"


def test_raise_for_private_maps_dns_resolution_failure():
    with patch(
        "app.url_import.socket.getaddrinfo",
        side_effect=socket.gaierror(8, "Name or service not known"),
    ):
        with pytest.raises(UrlImportError) as exc:
            raise_for_private_or_meta_hosts("missing.example.invalid")
    assert exc.value.status_code == 400
    assert "Could not resolve host" in exc.value.detail


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


class _FakeStreamResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeStreamResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        self.requests.append((method, url))
        return self._responses.pop(0)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns_infos(*_args, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 443),
        )
    ]


def test_fetch_rejects_redirect_without_location_header():
    from app.url_import import fetch_url_document

    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(302, {}),
        ]
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_infos
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Redirect without Location header"
    assert client.requests == [("GET", "https://example.com/start")]


def test_body_for_storage_rejects_unsupported_content_type():
    from app.url_import import _body_for_storage

    with pytest.raises(UrlImportError) as exc:
        _body_for_storage("unknown_binary", b"PK\x03\x04", "application/zip")
    assert exc.value.status_code == 415
    assert "Unsupported content type" in exc.value.detail
