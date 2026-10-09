"""SSRF DNS scan must keep walking records after ones it cannot parse."""

import asyncio
import socket
from types import SimpleNamespace

import pytest

from app.url_import import UrlImportError, fetch_url_document, raise_for_private_or_meta_hosts

_PUBLIC = "93.184.216.34"


def _settings():
    return SimpleNamespace(
        url_import_enabled=True,
        url_import_timeout_seconds=5.0,
        url_import_max_redirects=1,
        url_import_max_bytes=1024,
    )


def _unusable_then(address: str):
    """Records that `_ip_from_sockaddr` or `ip_address` skip, then one real IP."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", "not-a-tuple"),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ()),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (12345,)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("999.999.999.999", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)),
    ]


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


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "::1",
    ],
)
def test_unusable_dns_records_do_not_hide_a_later_disallowed_address(monkeypatch, address):
    """A garbage record must not stop the scan before a private, loopback, link-local, or multicast address."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "mixed.example"
        assert port is None
        assert kwargs.get("type") == socket.SOCK_STREAM
        return _unusable_then(address)

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("mixed.example")

    assert exc.value.status_code == 403
    assert exc.value.detail == "URL resolves to a disallowed address"


def test_fetch_does_not_get_when_a_later_dns_record_is_private(monkeypatch):
    client = _FakeAsyncClient(
        [_FakeResponse(headers={"content-type": "text/plain"}, chunks=(b"secret",))]
    )

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "mixed.example"
        assert port is None
        assert kwargs.get("type") == socket.SOCK_STREAM
        return _unusable_then("10.1.2.3")

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", lambda **_kwargs: client)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://mixed.example/secret", _settings()))

    assert exc.value.status_code == 403
    assert client.requested_urls == []


def test_fetch_allows_public_address_after_unusable_dns_records(monkeypatch):
    """Unparseable records are skipped; a later global address is still fetched."""
    client = _FakeAsyncClient(
        [_FakeResponse(headers={"content-type": "text/plain"}, chunks=(b"public-doc",))]
    )
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append((host, port, kwargs.get("type")))
        assert host == "mixed.example"
        return _unusable_then(_PUBLIC)

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", lambda **_kwargs: client)

    body, extension = asyncio.run(
        fetch_url_document("https://mixed.example/doc.txt", _settings())
    )

    assert body == b"public-doc"
    assert extension == ".txt"
    assert client.requested_urls == ["https://mixed.example/doc.txt"]
    assert calls == [("mixed.example", None, socket.SOCK_STREAM)]
