import logging
from unittest.mock import MagicMock, patch

from worker_app import processor
from worker_app.processor import notify_index, process_job_body

_TEXT = "DOC_MARKER_49eb_confidential_clause\nretention stays private"
_JOB_ID = "job-49eb"
_FILENAME = "reports/q1.txt"
_BASE = "http://index-host:8000/"
_INDEX_URL = "http://index-host:8000/internal/index"


def test_notify_index_posts_stripped_base_url_and_document_text(caplog, monkeypatch):
    monkeypatch.setattr(processor.settings, "ingestion_base_url", _BASE)
    caplog.set_level(logging.WARNING, logger="worker_app.processor")

    with patch("httpx.post", return_value=MagicMock()) as post:
        notify_index(_JOB_ID, _FILENAME, _TEXT)

    post.assert_called_once_with(
        _INDEX_URL,
        json={"job_id": _JOB_ID, "filename": _FILENAME, "text": _TEXT},
        timeout=30.0,
    )
    assert "index_notify_failed" not in caplog.text


def test_notify_failure_after_save_omits_document_text(caplog, monkeypatch):
    monkeypatch.setattr(processor.settings, "ingestion_base_url", _BASE)
    caplog.set_level(logging.WARNING, logger="worker_app.processor")
    table = MagicMock()
    events = []

    def record_save(**kwargs):
        events.append("save")

    def refuse_post(*args, **kwargs):
        events.append("post")
        raise RuntimeError("index upstream refused")

    table.put_item.side_effect = record_save
    with patch(
        "worker_app.processor.download_object_bytes", return_value=b"bytes"
    ), patch(
        "worker_app.processor.extract_text_from_object", return_value=_TEXT
    ), patch(
        "worker_app.processor.run_agent",
        return_value={"classification": "Legal", "summary": "kept"},
    ), patch("httpx.post", side_effect=refuse_post) as post:
        process_job_body(_JOB_ID, _FILENAME, table)

    assert events == ["save", "post"]
    table.put_item.assert_called_once_with(
        Item={
            "MessageId": _JOB_ID,
            "Status": "Completed",
            "Classification": "Legal",
            "Summary": "kept",
        }
    )
    post.assert_called_once_with(
        _INDEX_URL,
        json={"job_id": _JOB_ID, "filename": _FILENAME, "text": _TEXT},
        timeout=30.0,
    )
    assert "index_notify_failed: index upstream refused" in caplog.text
    assert "DOC_MARKER_49eb_confidential_clause" not in caplog.text
