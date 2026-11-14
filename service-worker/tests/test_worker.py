import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from worker_app.config import settings
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


def test_handle_message_claim_status_read_error_skips_process():
    """An unread claim status must not be processed or treated as a retry."""
    body = json.dumps(
        {
            "s3_key": "reports/unread-claim.txt",
            "idempotency_key": "job-claim-unread",
            "uploaded_at": "2024-01-12T00:00:00+00:00",
        }
    )
    msg = {"Body": body}
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "conditional claim lost",
            }
        },
        "PutItem",
    )
    unread = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "claim status unread"}},
        "GetItem",
    )
    table.get_item.side_effect = unread

    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.process_job_body"
    ) as proc:
        gr.return_value.Table.return_value = table
        with pytest.raises(ClientError) as exc:
            handle_message(msg)

    assert exc.value is unread
    gr.return_value.Table.assert_called_once_with(settings.dynamodb_table)
    table.get_item.assert_called_once_with(Key={"MessageId": "job-claim-unread"})
    proc.assert_not_called()


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
