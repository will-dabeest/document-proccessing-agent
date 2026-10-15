"""PDF magic sniffing is case-sensitive and only matches a leading %PDF-."""

from app.url_import import _classify_body


def test_lowercase_pdf_marker_is_not_magic():
    body = b"%pdf-1.7\n1 0 obj\n"

    assert _classify_body(None, body) == "unknown_binary"
    assert _classify_body("application/octet-stream", body) == "unknown_binary"
    assert _classify_body("text/plain", body) == "text"
