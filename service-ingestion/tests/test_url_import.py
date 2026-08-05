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


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_cgnat_literal():
    """100.64/10 is not is_private; must still be rejected via not-is_global."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("100.64.0.1")
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_raise_for_private_blocks_cgnat_via_dns(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "cgnat.example"
        return [
            (0, 0, 0, "", ("100.64.0.1", 0)),
        ]

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("cgnat.example")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_decimal_cgnat_hostname():
    """Decimal IPv4 hostnames can resolve to CGNAT (e.g. 1681915905 → 100.64.0.1)."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("1681915905")
    assert exc.value.status_code == 403


def test_raise_for_private_allows_public_literal():
    raise_for_private_or_meta_hosts("8.8.8.8")


def test_raise_for_private_rejects_empty_getaddrinfo(monkeypatch):
    monkeypatch.setattr("app.url_import.socket.getaddrinfo", lambda *a, **k: [])
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("empty-resolve.example")
    assert exc.value.status_code == 400
    assert "resolve" in exc.value.detail.lower()


def test_raise_for_private_rejects_unusable_sockaddrs(monkeypatch):
    """Fail closed when getaddrinfo returns no parseable IP strings."""
    monkeypatch.setattr(
        "app.url_import.socket.getaddrinfo",
        lambda *a, **k: [
            (0, 0, 0, "", (None, 0)),
            (0, 0, 0, "", (12345, 0)),
        ],
    )
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("bad-sockaddr.example")
    assert exc.value.status_code == 400


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
