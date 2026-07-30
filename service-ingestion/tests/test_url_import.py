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


@pytest.mark.parametrize(
    "hostname",
    [
        "224.0.0.1",  # multicast
        "100.64.0.1",  # CGNAT / shared address space (treated as private)
        "fc00::1",  # IPv6 unique local
        "[fc00::1]",
    ],
)
def test_raise_for_private_blocks_multicast_cgnat_and_ula(hostname):
    """Cover SSRF host classes beyond loopback/RFC1918 already asserted on main."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_parse_rejects_javascript_and_ftp_schemes():
    for url in ("javascript:alert(1)", "ftp://example.com/a.txt"):
        with pytest.raises(UrlImportError) as exc:
            _parse_and_validate_url(url)
        assert exc.value.status_code == 400
        assert "http" in exc.value.detail.lower()


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
