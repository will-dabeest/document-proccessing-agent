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


def test_raise_for_private_blocks_resolved_private_address(monkeypatch):
    import socket

    def fake_getaddrinfo(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("docs.example")

    assert exc.value.status_code == 403


def test_raise_for_private_resolution_failure_returns_bad_request(monkeypatch):
    import socket

    def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("missing.example")

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


def test_body_for_storage_preserves_markdown_extension():
    body, ext = _body_for_storage("text", b"# Title\n", "text/markdown; charset=utf-8")

    assert body == b"# Title\n"
    assert ext == ".md"


def test_body_for_storage_rejects_unknown_binary():
    with pytest.raises(UrlImportError) as exc:
        _body_for_storage("unknown_binary", b"PK\x03\x04", "application/zip")

    assert exc.value.status_code == 415
