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


@pytest.mark.parametrize("host", ["192.0.0.170", "192.0.0.171"])
def test_raise_for_private_blocks_nat64_discovery_literals(host):
    """RFC 7050 well-known addresses are an explicit private 192.0.0.170/31.

    They sit inside 192.0.0.0/24 but are not the globally reachable exceptions
    192.0.0.9 and 192.0.0.10. Both ends of the /31 must 403.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    address = ipaddress.ip_address(parsed_host)
    assert address.is_private
    assert not address.is_global
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "192.0.0.0252",  # octal last octet -> 192.0.0.170
        "192.0.0.0253",  # octal last octet -> 192.0.0.171
        "192.0.0.0xaa",  # hex last octet -> 192.0.0.170
        "192.0.0.0xab",  # hex last octet -> 192.0.0.171
        "192.0.0252",  # three-part octal -> 192.0.0.170
        "0xc00000aa",  # hex integer -> 192.0.0.170
        "3221225642",  # decimal integer -> 192.0.0.170
    ],
)
def test_raise_for_private_blocks_encoded_nat64_discovery_hosts(host):
    """ipaddress rejects these spellings; glibc still maps them onto 192.0.0.170/31.

    A literal-only SSRF check would allow the import.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{host}/secret")
    assert parsed_host == host
    with pytest.raises(ValueError):
        ipaddress.ip_address(parsed_host)
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    ("url_host", "is_private", "is_reserved"),
    [
        ("[::ffff:192.0.0.170]", True, True),
        ("[::ffff:0:192.0.0.170]", False, True),
        ("[2002:c000:aa::]", True, False),
        ("[64:ff9b::192.0.0.170]", False, True),
        ("[2002:c000:ab::]", True, False),
        ("[64:ff9b::192.0.0.171]", False, True),
    ],
)
def test_raise_for_private_blocks_ipv6_aliases_of_nat64_discovery(
    url_host, is_private, is_reserved
):
    """IPv6 spellings of 192.0.0.170/31 stay blocked after bracket stripping.

    Mapped ::ffff:0:192.0.0.170 and NAT64 64:ff9b:: are reserved and still
    globally reachable, so the check has to honor is_reserved. 6to4
    2002:c000:aa:: / 2002:c000:ab:: is private.
    """
    _raw, parsed_host, _port = _parse_and_validate_url(f"https://{url_host}/secret")
    address = ipaddress.ip_address(parsed_host)
    assert address.is_private is is_private
    assert address.is_reserved is is_reserved
    if not is_private:
        assert address.is_global
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(parsed_host)
    assert exc.value.status_code == 403


def test_fetch_octal_nat64_discovery_is_blocked_before_get():
    """192.0.0.0252 is an ipaddress miss; glibc maps it to 192.0.0.170."""
    _assert_fetch_blocked_before_get("https://192.0.0.0252/secret")


def test_fetch_reserved_mapped_nat64_discovery_is_blocked_before_get():
    """::ffff:0:192.0.0.170 is reserved and not private. Fetch must still 403 before GET."""
    _assert_fetch_blocked_before_get("https://[::ffff:0:192.0.0.170]/secret")
