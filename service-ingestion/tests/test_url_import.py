import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)

# Character length, not UTF-8 bytes. Hard-coded so a changed limit fails.
_URL_LENGTH_LIMIT = 2048
_URL_LENGTH_PREFIX = "https://example.com/"


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


def _url_of_length(n: int, *, last: str = "Z") -> str:
    pad = n - len(_URL_LENGTH_PREFIX) - len(last)
    assert pad >= 0
    url = _URL_LENGTH_PREFIX + ("a" * pad) + last
    assert len(url) == n
    return url


def test_parse_url_length_allows_2048_chars_after_strip():
    # "é" is one character and two UTF-8 bytes (a byte limit would reject).
    url = _url_of_length(_URL_LENGTH_LIMIT, last="é")
    assert len(url.encode("utf-8")) == _URL_LENGTH_LIMIT + 1

    raw, host, port = _parse_and_validate_url(f"\n{url} \t")

    assert raw == url
    assert raw.endswith("é")
    assert host == "example.com"
    assert port is None


def test_parse_url_length_rejects_one_past_2048():
    url = _url_of_length(_URL_LENGTH_LIMIT + 1)
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(url)
    assert exc.value.status_code == 400
    assert exc.value.detail == "URL is too long"


def test_blank_url_longer_than_limit_is_required_not_too_long():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(" " * (_URL_LENGTH_LIMIT + 500))
    assert exc.value.status_code == 400
    assert exc.value.detail == "URL is required"


def test_fetch_overlong_url_stops_before_dns_and_get():
    url = _url_of_length(_URL_LENGTH_LIMIT + 1)
    settings = SimpleNamespace(
        url_import_enabled=True,
        url_import_timeout_seconds=5.0,
        url_import_max_redirects=0,
        url_import_max_bytes=1024,
    )

    def _dns_must_not_run(*_args, **_kwargs):
        raise AssertionError("getaddrinfo should not run for an overlong URL")

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            raise AssertionError("HTTP GET should not run for an overlong URL")

    with patch(
        "app.url_import.socket.getaddrinfo", side_effect=_dns_must_not_run
    ), patch("app.url_import.httpx.AsyncClient", _Client):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document(url, settings))

    assert exc.value.status_code == 400
    assert exc.value.detail == "URL is too long"
