from unittest.mock import call, patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _html_to_plain_text,
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
    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_html_to_plain_text_retries_empty_extraction_with_favor_recall():
    raw = b"<html><body><main>Recoverable content</main></body></html>"

    with patch(
        "app.url_import.trafilatura.extract",
        side_effect=["   ", "  Recovered document text  "],
    ) as extract:
        out = _html_to_plain_text(raw)

    assert out == b"Recovered document text"
    assert extract.call_args_list == [
        call(raw.decode("utf-8"), output_format="txt"),
        call(raw.decode("utf-8"), output_format="txt", favor_recall=True),
    ]


def test_html_to_plain_text_replaces_invalid_utf8_before_extraction():
    with patch(
        "app.url_import.trafilatura.extract", return_value="Normalized text"
    ) as extract:
        out = _html_to_plain_text(b"<p>broken: \xff</p>")

    assert out == b"Normalized text"
    extract.assert_called_once_with("<p>broken: \ufffd</p>", output_format="txt")
