"""A successful strict HTML extract must be stored as-is.

favor_recall is the fallback for an empty first pass. Using it whenever it
returns text would replace the stricter article extract with boilerplate.
"""

from unittest.mock import patch

from app.url_import import _html_to_plain_text


def test_html_to_plain_text_keeps_stripped_first_extract_and_skips_recall():
    raw = b"<html><body><p>Primary article</p></body></html>"

    with patch(
        "app.url_import.trafilatura.extract",
        return_value="  Primary article  ",
    ) as extract:
        out = _html_to_plain_text(raw)

    assert out == b"Primary article"
    extract.assert_called_once_with(raw.decode("utf-8"), output_format="txt")
