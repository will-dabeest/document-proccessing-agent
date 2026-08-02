import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
    _classify_body,
    _normalize_text_body,
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


def test_classify_pdf_magic_overrides_octet_stream():
    assert _classify_body("application/octet-stream", b"%PDF-1.4\n1 0 obj") == "pdf"


def test_classify_html():
    assert _classify_body("text/html; charset=utf-8", b"<html></html>") == "html"


def test_classify_pdf_by_content_type_without_magic():
    assert _classify_body("application/pdf", b"not-magic-pdf-bytes") == "pdf"
    assert _classify_body("application/x-pdf", b"still-not-magic") == "pdf"


def test_classify_xhtml_as_html():
    assert (
        _classify_body("application/xhtml+xml; charset=utf-8", b"<html/>") == "html"
    )


def test_classify_unknown_binary():
    assert _classify_body("application/zip", b"PK\x03\x04") == "unknown_binary"


def test_normalize_text_body_replaces_invalid_utf8():
    out = _normalize_text_body(b"ok\xffmore")
    assert b"ok" in out
    assert b"more" in out
    assert out.decode("utf-8")  # replacement chars make it valid UTF-8


def test_body_for_storage_text_plain_uses_txt_and_normalizes():
    body, ext = _body_for_storage("text", b"hello\xffworld", "text/plain")
    assert ext == ".txt"
    assert body.decode("utf-8").startswith("hello")


def test_body_for_storage_pdf_keeps_raw_bytes():
    raw = b"%PDF-1.4 not a real pdf but kept"
    body, ext = _body_for_storage("pdf", raw, "application/pdf")
    assert ext == ".pdf"
    assert body == raw


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out
