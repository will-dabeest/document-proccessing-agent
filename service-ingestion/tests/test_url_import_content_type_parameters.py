"""Content-Type parameters must not override the media type used for storage.

Servers often send ``filename="notes.pdf"`` or ``format=markdown`` on a different
type. Classification uses only the type before the first semicolon, so those
parameters cannot force a PDF object key or a markdown extension.
"""

from unittest.mock import patch

import pytest

from app.url_import import UrlImportError, _body_for_storage, _classify_body


def test_filename_pdf_parameter_keeps_plain_text_as_txt():
    content_type = 'text/plain; filename="notes.pdf"'
    body = b"retention policy"
    assert _classify_body(content_type, body) == "text"
    stored, ext = _body_for_storage("text", body, content_type)
    assert ext == ".txt"
    assert stored == body


def test_markdown_format_parameter_on_plain_text_keeps_txt_extension():
    content_type = "text/plain; format=markdown"
    body = b"# heading\n"
    assert _classify_body(content_type, body) == "text"
    stored, ext = _body_for_storage("text", body, content_type)
    assert ext == ".txt"
    assert stored == body


def test_filename_pdf_parameter_on_markdown_keeps_md_extension():
    content_type = 'text/markdown; filename="notes.pdf"'
    body = b"# heading\n"
    assert _classify_body(content_type, body) == "text"
    stored, ext = _body_for_storage("text", body, content_type)
    assert ext == ".md"
    assert stored == body


def test_octet_stream_filename_pdf_without_magic_is_rejected():
    content_type = 'application/octet-stream; filename="notes.pdf"'
    body = b"not a pdf"
    assert _classify_body(content_type, body) == "unknown_binary"
    with pytest.raises(UrlImportError) as exc:
        _body_for_storage("unknown_binary", body, content_type)
    assert exc.value.status_code == 415
    assert exc.value.detail.startswith("Unsupported content type")


def test_pdf_magic_wins_over_filename_txt_parameter():
    content_type = 'application/octet-stream; filename="notes.txt"'
    body = b"%PDF-1.7\n1 0 obj"
    assert _classify_body(content_type, body) == "pdf"
    stored, ext = _body_for_storage("pdf", body, content_type)
    assert ext == ".pdf"
    assert stored == body


def test_html_with_pdf_filename_parameter_is_stored_as_txt():
    content_type = 'text/html; filename="page.pdf"'
    body = b"<html><body><p>Visible article</p></body></html>"
    assert _classify_body(content_type, body) == "html"
    with patch(
        "app.url_import.trafilatura.extract", return_value="Visible article"
    ) as extract:
        stored, ext = _body_for_storage("html", body, content_type)
    assert ext == ".txt"
    assert stored == b"Visible article"
    assert extract.call_count == 1
