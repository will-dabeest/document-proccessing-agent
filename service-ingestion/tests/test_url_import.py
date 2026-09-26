import asyncio
import ipaddress
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


def _url_import_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses, requests):
        self._responses = list(responses)
        self._requests = requests

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self._requests.append((method, url))
        return self._responses.pop(0)


def _assert_fetch_blocked_before_get(url: str) -> None:
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document(url, _url_import_settings()))
    assert exc.value.status_code == 403
    assert requests == []


@pytest.mark.parametrize("host", ["192.0.0.8", "192.0.0.11"])
def test_raise_for_private_blocks_special_purpose_hosts_around_global_exceptions(host):
    """192.0.0.0/24 is not globally reachable except 192.0.0.9 and 192.0.0.10.

    .8 is the IPv4 dummy address just past 192.0.0.0/29. .11 is the first
    address after the two globally reachable exceptions. Both must 403.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("host", ["192.0.0.9", "192.0.0.10"])
def test_raise_for_private_allows_iana_globally_reachable_exceptions(host):
    """PCP anycast .9 and NAT64/DNS64 discovery .10 stay reachable.

    They are the only exceptions inside 192.0.0.0/24. Blocking the whole
    /24 would reject these addresses.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/doc")
    assert parsed_host == host
    address = ipaddress.ip_address(parsed_host)
    assert address.is_global
    assert not address.is_private
    assert raise_for_private_or_meta_hosts(parsed_host) is None


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0.010",  # octal last octet -> 192.0.0.8
        "192.0.0.013",  # octal last octet -> 192.0.0.11
        "192.0.0.0x8",  # hex last octet -> 192.0.0.8
        "192.0.0.0xb",  # hex last octet -> 192.0.0.11
        "192.0.010",  # three-part octal -> 192.0.0.8
        "0xc0000008",  # hex integer -> 192.0.0.8
    ],
)
def test_raise_for_private_blocks_encoded_special_purpose_hosts(host):
    """ipaddress rejects these spellings; glibc still maps them onto 192.0.0.8 or .11.

    The SSRF check has to use the resolver result. A literal-only check would
    allow the import.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(ValueError):
        ipaddress.ip_address(parsed_host)
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0.011",  # octal last octet -> 192.0.0.9
        "192.0.0.012",  # octal last octet -> 192.0.0.10
        "192.0.0.0x9",  # hex last octet -> 192.0.0.9
        "192.0.0.0xa",  # hex last octet -> 192.0.0.10
    ],
)
def test_raise_for_private_allows_encoded_global_exceptions(host):
    """The same octal and hex spellings that land on .9 or .10 stay allowed.

    Adjacent encodings .010 and .013 are blocked. These four must not be.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/doc")
    assert parsed_host == host
    with pytest.raises(ValueError):
        ipaddress.ip_address(parsed_host)
    assert raise_for_private_or_meta_hosts(parsed_host) is None


def test_fetch_octal_dummy_address_is_blocked_before_get():
    """192.0.0.010 is an ipaddress miss; glibc maps it to 192.0.0.8.

    Fetch must 403 before GET.
    """
    _assert_fetch_blocked_before_get("https://192.0.0.010/secret")


def test_fetch_octal_global_exception_reaches_get():
    """192.0.0.011 resolves to globally reachable 192.0.0.9 and may be fetched."""
    requests: list[tuple[str, str]] = []
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"public-anycast",),
            )
        ],
        requests,
    )
    with patch("app.url_import.httpx.AsyncClient", client):
        body, ext = asyncio.run(
            fetch_url_document("https://192.0.0.011/doc", _url_import_settings())
        )
    assert requests == [("GET", "https://192.0.0.011/doc")]
    assert body == b"public-anycast"
    assert ext == ".txt"
