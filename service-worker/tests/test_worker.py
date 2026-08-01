import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

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


@mock_aws
def test_stuck_processing_claim_blocks_ack_on_redelivery(monkeypatch):
    """Failed process after claim leaves Status=Processing; redelivery must not delete."""
    import worker_app.config as cfg

    monkeypatch.setattr(cfg.settings, "use_localstack", False)
    monkeypatch.setattr(cfg.settings, "dynamodb_table", "ProcessLog")
    monkeypatch.setattr(cfg.settings, "aws_region", "us-east-1")

    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="ProcessLog",
        KeySchema=[{"AttributeName": "MessageId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "MessageId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ProcessLog")
    job_id = "job-stuck-1"
    body = json.dumps(
        {
            "s3_key": "stuck.txt",
            "idempotency_key": job_id,
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    msg = {"Body": body, "ReceiptHandle": "rh-stuck"}

    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.process_job_body",
        side_effect=RuntimeError("extract failed"),
    ) as proc:
        gr.return_value.Table.return_value = table
        with pytest.raises(RuntimeError, match="extract failed"):
            handle_message(msg)

        assert table.get_item(Key={"MessageId": job_id})["Item"]["Status"] == "Processing"
        assert proc.call_count == 1

        sqs = MagicMock()
        sqs.receive_message.return_value = {"Messages": [msg]}
        assert run_once(sqs, "http://q") is True
        sqs.delete_message.assert_not_called()
        assert proc.call_count == 1
        assert table.get_item(Key={"MessageId": job_id})["Item"]["Status"] == "Processing"
