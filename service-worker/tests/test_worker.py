import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from worker_app.tracing import extract_trace_from_message
from worker_app.worker import handle_message, main, run_once


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


def test_main_polls_configured_queue_and_retries_same_client(caplog):
    """A loop error must back off and poll the same queue, not exit or switch clients."""
    sqs = MagicMock(name="sqs-client")
    queue = "http://localhost:4566/000000000000/coverage-jobs"
    events: list[object] = []

    def configure_tracing() -> None:
        events.append("trace")

    def get_client():
        events.append("client")
        return sqs

    def poll(client, queue_url):
        events.append(("poll", client, queue_url))
        polls = [event for event in events if isinstance(event, tuple) and event[0] == "poll"]
        if len(polls) == 1:
            raise RuntimeError("sqs blip")
        raise KeyboardInterrupt()

    def sleep(seconds):
        events.append(("sleep", seconds))

    with caplog.at_level(logging.INFO, logger="worker_app.worker"), patch(
        "worker_app.worker._configure_tracing", side_effect=configure_tracing
    ), patch("worker_app.worker.get_sqs_client", side_effect=get_client), patch(
        "worker_app.worker.settings.queue_url", queue
    ), patch(
        "worker_app.worker.run_once", side_effect=poll
    ), patch(
        "worker_app.worker.time.sleep", side_effect=sleep
    ):
        main()

    assert events == [
        "trace",
        "client",
        ("poll", sqs, queue),
        ("sleep", 2),
        ("poll", sqs, queue),
    ]
    messages = [record.getMessage() for record in caplog.records]
    assert f"worker_started queue={queue}" in messages
    assert "worker_stopping" in messages
    loop_errors = [
        record for record in caplog.records if record.getMessage() == "worker_loop_error"
    ]
    assert len(loop_errors) == 1
    assert loop_errors[0].exc_info is not None
    assert "sqs blip" in str(loop_errors[0].exc_info[1])
