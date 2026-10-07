"""Size cap must follow streamed bytes, not the Content-Length header."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.url_import import UrlImportError, fetch_url_document


class _FakeResponse:
    def __init__(self, *, headers, chunks):
        self.status_code = 200
        self.headers = headers
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url):
        assert method == "GET"
        self.requested_urls.append(url)
        return self._response


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 5.0,
        "url_import_max_redirects": 0,
        "url_import_max_bytes": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns():
    return patch(
        "app.url_import.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 0),
            )
        ],
    )


def test_fetch_size_cap_rejects_body_when_content_length_understates_it():
    """A small Content-Length must not waive the streamed-byte cap."""
    client = _FakeAsyncClient(
        _FakeResponse(
            headers=httpx.Headers(
                {"content-type": "text/plain", "content-length": "1"}
            ),
            chunks=(b"abcd", b"efgh"),
        )
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), _public_dns():
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/over", _settings())
            )

    assert exc.value.status_code == 413
    assert exc.value.detail == "Response body exceeds configured limit"
    assert client.requested_urls == ["https://example.com/over"]


def test_fetch_size_cap_accepts_body_when_content_length_overstates_it():
    """A Content-Length above the cap must not reject a smaller streamed body."""
    client = _FakeAsyncClient(
        _FakeResponse(
            headers=httpx.Headers(
                {"content-type": "text/plain", "content-length": "99999"}
            ),
            chunks=(b"ok",),
        )
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), _public_dns():
        body, extension = asyncio.run(
            fetch_url_document("https://example.com/under", _settings())
        )

    assert body == b"ok"
    assert extension == ".txt"
    assert client.requested_urls == ["https://example.com/under"]
