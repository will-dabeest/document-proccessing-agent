"""Object-key suffix chooses the worker decoder, including when bytes look like a PDF."""

from io import BytesIO
from unittest.mock import MagicMock, patch

from worker_app.processor import process_job_body

PDF_MAGIC_TXT = b"%PDF-1.7\nDOC_MARKER_extension_wins_txt\n"
PDF_MAGIC_TXT_KEY = "reports/extension-wins-pdf-magic.txt"
PDF_MAGIC_TXT_JOB = "job-extension-wins-txt"

UPPER_PDF_BODY = b"%PDF-1.7\nDOC_MARKER_extension_wins_pdf_bytes\n"
UPPER_PDF_KEY = "reports/extension-wins-upper.PDF"
UPPER_PDF_JOB = "job-extension-wins-pdf"
UPPER_PDF_PAGE = "DOC_MARKER_extension_wins_pdf_page"


def test_txt_pdf_magic_is_indexed_as_utf8_without_pdf_reader():
    table = MagicMock()
    expected = PDF_MAGIC_TXT.decode("utf-8")
    with patch(
        "worker_app.processor.download_object_bytes", return_value=PDF_MAGIC_TXT
    ), patch("worker_app.processor.PdfReader") as pdf, patch(
        "worker_app.processor.run_agent",
        return_value={"classification": "Notice", "summary": "extension-wins-txt"},
    ) as agent, patch(
        "worker_app.processor.notify_index"
    ) as notify:
        process_job_body(PDF_MAGIC_TXT_JOB, PDF_MAGIC_TXT_KEY, table)

    pdf.assert_not_called()
    state = agent.call_args[0][0]
    assert state["document_text"] == expected
    assert state["document_text"].startswith("%PDF-")
    assert state["job_id"] == PDF_MAGIC_TXT_JOB
    assert state["s3_key"] == PDF_MAGIC_TXT_KEY
    notify.assert_called_once_with(PDF_MAGIC_TXT_JOB, PDF_MAGIC_TXT_KEY, expected)
    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["MessageId"] == PDF_MAGIC_TXT_JOB
    assert item["Status"] == "Completed"


def test_uppercase_pdf_key_indexes_pdf_reader_text():
    page = MagicMock()
    page.extract_text.return_value = UPPER_PDF_PAGE
    reader = MagicMock()
    reader.pages = [page]
    table = MagicMock()
    with patch(
        "worker_app.processor.download_object_bytes", return_value=UPPER_PDF_BODY
    ), patch(
        "worker_app.processor.PdfReader", return_value=reader
    ) as pdf, patch(
        "worker_app.processor.run_agent",
        return_value={"classification": "Notice", "summary": "extension-wins-pdf"},
    ) as agent, patch(
        "worker_app.processor.notify_index"
    ) as notify:
        process_job_body(UPPER_PDF_JOB, UPPER_PDF_KEY, table)

    pdf.assert_called_once()
    (stream,) = pdf.call_args.args
    assert isinstance(stream, BytesIO)
    assert stream.getvalue() == UPPER_PDF_BODY
    page.extract_text.assert_called_once_with()
    state = agent.call_args[0][0]
    assert state["document_text"] == UPPER_PDF_PAGE
    assert state["document_text"] != UPPER_PDF_BODY.decode("utf-8")
    assert state["job_id"] == UPPER_PDF_JOB
    assert state["s3_key"] == UPPER_PDF_KEY
    notify.assert_called_once_with(UPPER_PDF_JOB, UPPER_PDF_KEY, UPPER_PDF_PAGE)
    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["MessageId"] == UPPER_PDF_JOB
    assert item["Status"] == "Completed"
