import socket

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


def test_parse_accepts_https():
    raw, host, port = _parse_and_validate_url("https://example.com/path?q=1")
    assert host == "example.com"
    assert "example.com" in raw
    assert port is None


def test_parse_accepts_https_with_explicit_port():
    raw, host, port = _parse_and_validate_url("https://example.com:8443/docs")
    assert host == "example.com"
    assert port == 8443
    assert ":8443" in raw


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_mixed_public_and_private_dns(monkeypatch):
    """Any private A/AAAA record must 403 even when a public address is also returned."""

    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "dual.example"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ]

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("dual.example")
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_classify_pdf_magic_overrides_octet_stream():
    assert _classify_body("application/octet-stream", b"%PDF-1.4\n1 0 obj") == "pdf"


def test_classify_pdf_magic_when_content_type_missing():
    assert _classify_body(None, b"%PDF-1.7\n") == "pdf"
    assert _classify_body("", b"%PDF-1.7\n") == "pdf"


def test_classify_missing_content_type_without_pdf_magic_is_unknown_binary():
    assert _classify_body(None, b"hello text") == "unknown_binary"


def test_classify_html():
    assert _classify_body("text/html; charset=utf-8", b"<html></html>") == "html"


def test_classify_unknown_binary():
    assert _classify_body("application/zip", b"PK\x03\x04") == "unknown_binary"


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out
