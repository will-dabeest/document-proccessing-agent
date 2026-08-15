import socket
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


def _dns_result(ip: str):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, 0),
        )
    ]


def _parse_host(url: str) -> str:
    _raw, host, _port = _parse_and_validate_url(url)
    return host


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


@pytest.mark.parametrize(
    "url, expected_host",
    [
        ("HTTP://Example.COM/doc", "example.com"),
        ("HTTPS://Example.COM/doc", "example.com"),
    ],
)
def test_parse_accepts_uppercase_http_schemes(url, expected_host):
    raw, host, port = _parse_and_validate_url(url)
    assert host == expected_host
    assert port is None
    assert raw == url


def test_parse_userinfo_does_not_override_hostname():
    """Credentials that look like an IP must not be treated as the target host."""
    assert _parse_host("https://127.0.0.1@example.com/doc") == "example.com"
    assert _parse_host("https://user:pass@example.com/doc") == "example.com"


def test_parse_userinfo_on_loopback_still_targets_loopback():
    assert _parse_host("https://user:pass@127.0.0.1/secret") == "127.0.0.1"


def test_raise_for_private_blocks_userinfo_loopback_url():
    host = _parse_host("https://user:pass@127.0.0.1/secret")
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_allows_userinfo_on_public_hostname():
    host = _parse_host("https://127.0.0.1@example.com/doc")
    with patch(
        "app.url_import.socket.getaddrinfo",
        return_value=_dns_result("93.184.216.34"),
    ) as gai:
        raise_for_private_or_meta_hosts(host)
    gai.assert_called()
    assert gai.call_args[0][0] == "example.com"


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "hostname",
    [
        "::ffff:127.0.0.1",
        "[::ffff:10.0.0.1]",
        "fe80::1",
    ],
)
def test_raise_for_private_blocks_ipv4_mapped_and_link_local_ipv6(hostname):
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403


def test_parse_bracketed_ipv4_mapped_host_is_unbracketed():
    host = _parse_host("https://[::ffff:127.0.0.1]/x")
    assert host == "::ffff:127.0.0.1"
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


def test_raise_for_private_looks_up_idna_punycode_form():
    with patch(
        "app.url_import.socket.getaddrinfo",
        return_value=_dns_result("93.184.216.34"),
    ) as gai:
        raise_for_private_or_meta_hosts("münchen.example.com")
    assert gai.call_args[0][0] == "xn--mnchen-3ya.example.com"


def test_raise_for_private_blocks_idna_host_that_resolves_privately():
    with patch(
        "app.url_import.socket.getaddrinfo",
        return_value=_dns_result("127.0.0.1"),
    ) as gai:
        with pytest.raises(UrlImportError) as exc:
            raise_for_private_or_meta_hosts("münchen.example.com")
    assert gai.call_args[0][0] == "xn--mnchen-3ya.example.com"
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_trailing_dot_localhost_via_dns():
    """FQDN form localhost. is a distinct lookup from bare localhost."""
    with patch(
        "app.url_import.socket.getaddrinfo",
        return_value=_dns_result("127.0.0.1"),
    ) as gai:
        with pytest.raises(UrlImportError) as exc:
            raise_for_private_or_meta_hosts("localhost.")
    assert gai.call_args[0][0] == "localhost."
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
