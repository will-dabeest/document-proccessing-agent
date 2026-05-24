import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.url_import import UrlImportError, fetch_url_document


class FakeStreamResponse:
    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.request = None

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
        if self.status_code < 400:
            return

        request = self.request or httpx.Request("GET", "https://example.test/")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("remote error", request=request, response=response)


def _settings(max_bytes=100, max_redirects=3):
    return SimpleNamespace(
        url_import_enabled=True,
        url_import_max_bytes=max_bytes,
        url_import_max_redirects=max_redirects,
        url_import_timeout_seconds=1.0,
    )


def _install_fake_client(monkeypatch, responses):
    instances = []

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            self.requests = []
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url):
            self.requests.append(url)
            response = responses[url]
            response.request = httpx.Request(method, url)
            return response

    monkeypatch.setattr("app.url_import.httpx.AsyncClient", FakeAsyncClient)
    return instances


def _install_host_validator(monkeypatch, denied_hosts=frozenset()):
    seen_hosts = []

    def fake_validator(hostname):
        seen_hosts.append(hostname)
        if hostname in denied_hosts:
            raise UrlImportError(403, "URL resolves to a disallowed address")

    monkeypatch.setattr("app.url_import.raise_for_private_or_meta_hosts", fake_validator)
    return seen_hosts


def test_fetch_follows_relative_redirect_and_returns_markdown(monkeypatch):
    responses = {
        "https://example.test/start": FakeStreamResponse(
            302, {"location": "/final"}
        ),
        "https://example.test/final": FakeStreamResponse(
            200, {"content-type": "text/markdown; charset=utf-8"}, [b"# Title\n"]
        ),
    }
    clients = _install_fake_client(monkeypatch, responses)
    seen_hosts = _install_host_validator(monkeypatch)

    body, ext = asyncio.run(
        fetch_url_document(" https://example.test/start ", _settings())
    )

    assert body == b"# Title\n"
    assert ext == ".md"
    assert clients[0].requests == [
        "https://example.test/start",
        "https://example.test/final",
    ]
    assert seen_hosts == ["example.test", "example.test"]


def test_fetch_revalidates_redirect_target_before_following(monkeypatch):
    responses = {
        "https://example.test/start": FakeStreamResponse(
            302, {"location": "http://127.0.0.1/admin"}
        )
    }
    clients = _install_fake_client(monkeypatch, responses)
    seen_hosts = _install_host_validator(monkeypatch, {"127.0.0.1"})

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.test/start", _settings()))

    assert exc.value.status_code == 403
    assert clients[0].requests == ["https://example.test/start"]
    assert seen_hosts == ["example.test", "127.0.0.1"]


def test_fetch_enforces_streamed_max_bytes(monkeypatch):
    responses = {
        "https://example.test/big": FakeStreamResponse(
            200, {"content-type": "text/plain"}, [b"abcd", b"e"]
        )
    }
    _install_fake_client(monkeypatch, responses)
    _install_host_validator(monkeypatch)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document("https://example.test/big", _settings(max_bytes=4))
        )

    assert exc.value.status_code == 413


def test_fetch_remote_http_error_maps_to_bad_gateway(monkeypatch):
    responses = {
        "https://example.test/missing": FakeStreamResponse(404, {"content-type": "text/plain"})
    }
    _install_fake_client(monkeypatch, responses)
    _install_host_validator(monkeypatch)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.test/missing", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_too_many_redirects_returns_bad_gateway(monkeypatch):
    responses = {
        "https://example.test/one": FakeStreamResponse(302, {"location": "/two"}),
        "https://example.test/two": FakeStreamResponse(302, {"location": "/three"}),
    }
    clients = _install_fake_client(monkeypatch, responses)
    _install_host_validator(monkeypatch)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.test/one", _settings(max_redirects=1)
            )
        )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Too many redirects"
    assert clients[0].requests == [
        "https://example.test/one",
        "https://example.test/two",
    ]
