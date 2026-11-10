import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from worker_app.tracing import extract_trace_from_message
from worker_app.worker import handle_message, run_once


def test_extract_trace_from_message_without_traceparent():
    ctx = extract_trace_from_message({"MessageAttributes": {}})
    assert ctx is not None


def test_extract_trace_from_message_with_traceparent():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    msg = {"MessageAttributes": {"traceparent": {"StringValue": tp}}}
    ctx = extract_trace_from_message(msg)
    assert ctx is not None


def test_handle_message_duplicate_done_skips_process():
    body = json.dumps(
        {
            "s3_key": "f.txt",
            "idempotency_key": "jid-1",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    msg = {"Body": body}
    fake_table = object()

    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.try_claim_job", return_value="duplicate_done"
    ) as tj, patch("worker_app.worker.process_job_body") as proc:
        gr.return_value.Table.return_value = fake_table
        handle_message(msg)

    tj.assert_called_once_with(fake_table, "jid-1")
    proc.assert_not_called()


def test_handle_message_duplicate_inflight_raises():
    body = json.dumps(
        {
            "s3_key": "f.txt",
            "idempotency_key": "jid-2",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    msg = {"Body": body}

    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.try_claim_job", return_value="duplicate_inflight"
    ):
        gr.return_value.Table.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="duplicate_inflight_retry"):
            handle_message(msg)


def test_handle_message_claimed_runs_process():
    body = json.dumps(
        {
            "s3_key": "g.txt",
            "idempotency_key": "jid-3",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    msg = {"Body": body}
    tbl = MagicMock()

    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.try_claim_job", return_value="claimed"
    ) as tj, patch("worker_app.worker.process_job_body") as proc:
        gr.return_value.Table.return_value = tbl
        handle_message(msg)

    proc.assert_called_once_with("jid-3", "g.txt", tbl)


def test_run_once_false_when_no_messages():
    sqs = MagicMock()
    sqs.receive_message.return_value = {}
    assert run_once(sqs, "http://example/queue") is False
    sqs.delete_message.assert_not_called()


def test_run_once_deletes_on_success():
    body = json.dumps(
        {
            "s3_key": "a.txt",
            "idempotency_key": "id-z",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [{"ReceiptHandle": "rh-1", "Body": body}],
    }
    with patch("worker_app.worker.handle_message"):
        assert run_once(sqs, "http://q") is True
    sqs.delete_message.assert_called_once_with(
        QueueUrl="http://q", ReceiptHandle="rh-1"
    )


def test_run_once_no_delete_when_handle_message_raises():
    body = json.dumps(
        {
            "s3_key": "a.txt",
            "idempotency_key": "id-y",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [{"ReceiptHandle": "rh-2", "Body": body}],
    }
    with patch("worker_app.worker.handle_message", side_effect=RuntimeError("boom")):
        assert run_once(sqs, "http://q") is True
    sqs.delete_message.assert_not_called()


def test_run_once_malformed_body_stays_queued_and_skips_processing(caplog):
    """Invalid job JSON stays on the queue and is not claimed or indexed."""
    queue_url = "http://localhost:4566/000000000000/coverage-malformed"
    receipt = "rh-malformed-keep"
    message_id = "sqs-msg-malformed-keep"
    envelope_marker = "DOC_MARKER_malformed_envelope"
    # Long enough that a ValidationError display cannot contain the whole
    # payload. The full string in the log means the worker logged the body.
    raw_body = (
        '{"s3_key":"reports/DOC_MARKER_malformed_keep.txt",'
        '"idempotency_key":"job-malformed-keep",'
        '"uploaded_at":"2024-01-11T00:00:00+00:00",'
        '"padding":"' + ("x" * 240)
    )
    message = {
        "MessageId": message_id,
        "ReceiptHandle": receipt,
        "Body": raw_body,
        "MessageAttributes": {
            "document_name": {
                "DataType": "String",
                "StringValue": envelope_marker,
            }
        },
    }
    sqs = MagicMock()
    sqs.receive_message.return_value = {"Messages": [message]}

    with caplog.at_level(logging.DEBUG, logger="worker_app.worker"), patch(
        "worker_app.worker.get_dynamodb_resource"
    ) as dynamo, patch("worker_app.worker.try_claim_job") as claim, patch(
        "worker_app.worker.process_job_body"
    ) as proc:
        assert run_once(sqs, queue_url) is True

    dynamo.assert_not_called()
    claim.assert_not_called()
    proc.assert_not_called()
    sqs.delete_message.assert_not_called()

    failures = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(failures) == 1
    assert failures[0].getMessage().startswith("message_processing_failed:")
    assert "worker_loop_error" not in failures[0].getMessage()
    assert failures[0].exc_info is not None
    assert isinstance(failures[0].exc_info[1], ValidationError)
    assert "worker_loop_error" not in caplog.text
    assert raw_body not in caplog.text
    assert envelope_marker not in caplog.text
    assert receipt not in caplog.text
    assert message_id not in caplog.text
    assert queue_url not in caplog.text
