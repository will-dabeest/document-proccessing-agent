import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
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


@pytest.mark.parametrize(
    "host",
    (
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
        "[2001:4860:4860::8888]",
    ),
)
def test_raise_for_private_allows_public_literals(host):
    raise_for_private_or_meta_hosts(host)


def test_classify_vendor_pdf_type_without_magic():
    assert _classify_body("application/vnd.pdf", b"%not-a-pdf") == "pdf"


def test_classify_json_and_csv_are_unsupported_binary():
    assert _classify_body("application/json", b'{"a":1}') == "unknown_binary"
    assert _classify_body("text/csv", b"a,b\n1,2\n") == "unknown_binary"


def test_body_for_storage_plain_text_uses_txt_extension():
    body, ext = _body_for_storage("text", b"hello world", "text/plain")
    assert ext == ".txt"
    assert body == b"hello world"


def test_body_for_storage_rejects_unknown_binary_with_415():
    with pytest.raises(UrlImportError) as exc:
        _body_for_storage("unknown_binary", b'{"a":1}', "application/json")
    assert exc.value.status_code == 415
    assert "Unsupported content type" in exc.value.detail
