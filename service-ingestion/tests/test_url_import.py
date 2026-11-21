from unittest.mock import call, patch

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


def test_body_for_storage_keeps_empty_and_untrimmed_text_bytes():
    """Plain text and markdown stay byte-for-byte, including empty and padded bodies.

    The worker indexes a whitespace-only extract and skips a truly empty one.
    Rejecting or stripping these bodies would change that job.
    """
    assert _classify_body("text/plain", b"") == "text"
    empty_body, empty_ext = _body_for_storage("text", b"", "text/plain")
    assert empty_ext == ".txt"
    assert empty_body == b""

    plain = b" \n\tDOC_MARKER_plain_ws\t\n "
    assert _classify_body("text/plain; charset=utf-8", plain) == "text"
    stored, ext = _body_for_storage("text", plain, "text/plain; charset=utf-8")
    assert ext == ".txt"
    assert stored == plain

    markdown = "\n# DOC_MARKER_md_ws café\n\n".encode("utf-8")
    assert _classify_body("text/x-markdown", markdown) == "text"
    md_body, md_ext = _body_for_storage("text", markdown, "text/x-markdown")
    assert md_ext == ".md"
    assert md_body == markdown


def test_html_recall_whitespace_is_stored_as_empty_txt():
    """Whitespace from the recall pass is stripped, so a blank page is an empty .txt.

    Empty bytes skip worker indexing. Keeping the whitespace would classify a blank page.
    """
    raw = b"<html><body><article>DOC_MARKER_recall_blank</article></body></html>"
    assert _classify_body("text/html; charset=utf-8", raw) == "html"
    with patch(
        "app.url_import.trafilatura.extract",
        side_effect=[None, "\r\n \t"],
    ) as extract:
        body, ext = _body_for_storage("html", raw, "text/html; charset=utf-8")

    assert body == b""
    assert ext == ".txt"
    decoded = raw.decode("utf-8")
    assert extract.call_args_list == [
        call(decoded, output_format="txt"),
        call(decoded, output_format="txt", favor_recall=True),
    ]
