"""UTF-8 BOM must not change what URL import stores.

Windows editors prefix text and HTML with EF BB BF. Decoding with
utf-8-sig would drop that prefix and still succeed. Plain text is stored
as the original UTF-8 bytes. HTML is extracted to article text; the BOM
must not hide the article or be written into the stored body.
"""

from app.url_import import _body_for_storage, _classify_body

_PLAIN = "\ufeffDOC_MARKER_utf8_bom_plain café\n".encode("utf-8")
_HTML = (
    "\ufeff<html><body><article><p>DOC_MARKER_utf8_bom_html</p></article></body></html>"
).encode("utf-8")


def test_plain_text_keeps_leading_utf8_bom_bytes():
    assert _PLAIN.startswith(b"\xef\xbb\xbf")
    assert _classify_body("text/plain; charset=utf-8", _PLAIN) == "text"

    stored, ext = _body_for_storage("text", _PLAIN, "text/plain; charset=utf-8")

    assert ext == ".txt"
    assert stored == _PLAIN


def test_html_with_leading_utf8_bom_stores_extracted_article():
    assert _classify_body("text/html; charset=utf-8", _HTML) == "html"

    stored, ext = _body_for_storage("html", _HTML, "text/html; charset=utf-8")

    assert ext == ".txt"
    assert stored == b"DOC_MARKER_utf8_bom_html"
