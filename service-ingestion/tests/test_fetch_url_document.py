import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import url_import
from app.url_import import UrlImportError, fetch_url_document


def _settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 5.0,
        "url_import_max_redirects": 3,
        "url_import_max_bytes": 1024,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeStreamResponse:
    def __init__(self, status_code=200, *, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.url = "https://example.com/"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code < 400:
            return
        request = httpx.Request("GET", self.url)
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("remote error", request=request, response=response)


class FakeAsyncClient:
    def __init__(self, responses, requests):
        self._responses = list(responses)
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self._requests.append((method, url))
        response = self._responses.pop(0)
        response.url = url
        return response


def _install_fake_client(monkeypatch, responses):
    requests = []

    def fake_client(*_args, **_kwargs):
        return FakeAsyncClient(responses, requests)

    monkeypatch.setattr(url_import.httpx, "AsyncClient", fake_client)
    return requests


def _resolve_example_hosts(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "example.com"
        return [(None, None, None, None, ("93.184.216.34", 0))]

    monkeypatch.setattr(url_import.socket, "getaddrinfo", fake_getaddrinfo)


def test_fetch_blocks_redirect_to_metadata_before_following(monkeypatch):
    _resolve_example_hosts(monkeypatch)
    requests = _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            ),
            FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"secret"],
            ),
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert requests == [("GET", "https://example.com/start")]


def test_fetch_enforces_streamed_body_size_limit(monkeypatch):
    _resolve_example_hosts(monkeypatch)
    _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"1234", b"5"],
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large",
                _settings(url_import_max_bytes=4),
            )
        )

    assert exc.value.status_code == 413


def test_fetch_maps_remote_status_to_bad_gateway(monkeypatch):
    _resolve_example_hosts(monkeypatch)
    _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(
                404,
                headers={"content-type": "text/plain"},
                chunks=[b"missing"],
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_follows_relative_redirect_and_preserves_markdown_extension(monkeypatch):
    _resolve_example_hosts(monkeypatch)
    requests = _install_fake_client(
        monkeypatch,
        [
            FakeStreamResponse(302, headers={"location": "/docs/readme"}),
            FakeStreamResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nBody"],
            ),
        ],
    )

    body, ext = asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert body == b"# Title\n\nBody"
    assert ext == ".md"
    assert requests == [
        ("GET", "https://example.com/start"),
        ("GET", "https://example.com/docs/readme"),
    ]
