"""Backslash before '@' must not hide the real URL host from the SSRF check."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import UrlImportError, _parse_and_validate_url, fetch_url_document

_LOOPBACK_URLS = (
    r"https://example.com\@127.0.0.1/secret",
    "https://example.com%5C@127.0.0.1/secret",
    "https://@127.0.0.1/secret",
)


def _settings():
    return SimpleNamespace(
        url_import_enabled=True,
        url_import_timeout_seconds=5.0,
        url_import_max_redirects=2,
        url_import_max_bytes=1024,
    )


class _FakeResponse:
    def __init__(self, *, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
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
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested_urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url):
        assert method == "GET"
        self.requested_urls.append(url)
        if not self._responses:
            raise AssertionError(f"unexpected GET {url}")
        return self._responses.pop(0)


def _public_dns(host, *_args, **_kwargs):
    assert host == "example.com"
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 0),
        )
    ]


@pytest.mark.parametrize("url", _LOOPBACK_URLS)
def test_parse_backslash_and_empty_userinfo_keep_loopback_host(url):
    """The host after '@' is the target, including a backslash or empty userinfo."""
    _raw, host, _port = _parse_and_validate_url(url)
    assert host == "127.0.0.1"


@pytest.mark.parametrize("url", _LOOPBACK_URLS)
def test_fetch_blocks_loopback_hidden_by_userinfo_before_get(url):
    """A public-looking prefix must not cause a GET to a loopback host."""
    client = _FakeAsyncClient([])

    def _dns_must_not_run(*_args, **_kwargs):
        raise AssertionError("literal loopback must be rejected before DNS")

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_dns_must_not_run
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document(url, _settings()))

    assert exc.value.status_code == 403
    assert exc.value.detail == "URL resolves to a disallowed address"
    assert client.requested_urls == []


def test_fetch_blocks_backslash_userinfo_redirect_before_second_get():
    """Location is reparsed; a backslash must not smuggle 127.0.0.1 onto the next hop."""
    start = "https://example.com/start"
    location = r"https://cdn.example\@127.0.0.1/secret"
    client = _FakeAsyncClient(
        [
            _FakeResponse(status_code=302, headers={"location": location}),
            _FakeResponse(headers={"content-type": "text/plain"}, chunks=(b"nope",)),
        ]
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document(start, _settings()))

    assert exc.value.status_code == 403
    assert client.requested_urls == [start]


def test_fetch_backslash_userinfo_uses_host_after_at():
    """127.0.0.1\\@example.com is example.com for both parsing and the GET."""
    url = r"https://127.0.0.1\@example.com/doc.txt"
    _raw, host, _port = _parse_and_validate_url(url)
    assert host == "example.com"

    client = _FakeAsyncClient(
        [
            _FakeResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"public-doc",),
            )
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, extension = asyncio.run(fetch_url_document(url, _settings()))

    assert body == b"public-doc"
    assert extension == ".txt"
    assert client.requested_urls == [url]
