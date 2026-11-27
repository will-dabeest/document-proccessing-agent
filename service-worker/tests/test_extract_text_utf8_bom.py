"""Worker decode of a .txt object that starts with a UTF-8 BOM (EF BB BF).

Plain-text import stores those bytes unchanged. Extraction must use strict
UTF-8, so the classifier and the index notify see U+FEFF. utf-8-sig would
drop the BOM, and a BOM-only object would then look empty.
"""

from unittest.mock import MagicMock, patch

from worker_app.processor import extract_text_from_object, process_job_body

_BOM = b"\xef\xbb\xbf"
_MARKER = "WORKER_EXTRACT_BOM_KEEP"


def test_txt_leading_utf8_bom_is_kept_in_decoded_text():
    payload = f"{_MARKER}\n".encode("utf-8")
    body = _BOM + payload
    assert body.startswith(b"\xef\xbb\xbf")

    text = extract_text_from_object("inbox/WORKER_EXTRACT_BOM_KEEP.TXT", body)

    assert text == "\ufeff" + _MARKER + "\n"
    assert text == body.decode("utf-8")
    assert text != body.decode("utf-8-sig")
    assert text.encode("utf-8") == body


def test_bom_only_txt_is_indexed_and_not_treated_as_empty():
    table = MagicMock()
    key = "inbox/worker-bom-only.txt"
    job_id = "job-worker-bom-only"
    with patch(
        "worker_app.processor.download_object_bytes", return_value=_BOM
    ), patch(
        "worker_app.processor.run_agent",
        return_value={"classification": "Notice", "summary": "bom-only-kept"},
    ) as run_agent, patch(
        "worker_app.processor.save_completed"
    ) as save_completed, patch(
        "worker_app.processor.notify_index"
    ) as notify_index:
        process_job_body(job_id, key, table)

    state = run_agent.call_args[0][0]
    assert state["document_text"] == "\ufeff"
    assert state["document_text"] != "(empty)"
    assert state["job_id"] == job_id
    assert state["s3_key"] == key
    save_completed.assert_called_once_with(table, job_id, "Notice", "bom-only-kept")
    notify_index.assert_called_once_with(job_id, key, "\ufeff")
