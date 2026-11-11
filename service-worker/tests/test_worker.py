import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

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


def _worker_log_records(caplog):
    return [r for r in caplog.records if r.name == "worker_app.worker"]


def test_main_keyboard_interrupt_stops_without_sleep(monkeypatch, caplog):
    queue_url = "http://localhost:4566/000000000000/coverage-receive-stop"
    monkeypatch.setattr("worker_app.worker.settings.queue_url", queue_url)
    polls = []
    sqs = MagicMock()

    def receive_message(**kwargs):
        polls.append(kwargs.get("QueueUrl"))
        if len(polls) > 1:
            raise SystemExit("worker loop did not stop")
        raise KeyboardInterrupt()

    sqs.receive_message.side_effect = receive_message

    with patch("worker_app.worker._configure_tracing") as tracing, patch(
        "worker_app.worker.get_sqs_client", return_value=sqs
    ) as get_sqs, patch("worker_app.worker.time.sleep") as sleep, patch(
        "worker_app.worker.handle_message"
    ) as handle:
        with caplog.at_level(logging.INFO, logger="worker_app.worker"):
            try:
                main()
            except KeyboardInterrupt:
                pytest.fail("KeyboardInterrupt escaped the worker loop")
            except SystemExit as exc:
                pytest.fail(f"worker loop did not stop: {exc}")

    tracing.assert_called_once_with()
    get_sqs.assert_called_once_with()
    handle.assert_not_called()
    sleep.assert_not_called()
    sqs.delete_message.assert_not_called()
    assert polls == [queue_url]
    records = _worker_log_records(caplog)
    assert [(r.levelno, r.message) for r in records] == [
        (logging.INFO, f"worker_started queue={queue_url}"),
        (logging.INFO, "worker_stopping"),
    ]
    assert all(r.exc_info is None for r in records)


def test_main_retries_after_sqs_receive_error(monkeypatch, caplog):
    queue_url = "http://localhost:4566/000000000000/coverage-receive-retry"
    monkeypatch.setattr("worker_app.worker.settings.queue_url", queue_url)
    events = []
    sqs = MagicMock()
    receive_error = ClientError(
        {
            "Error": {
                "Code": "ServiceUnavailable",
                "Message": "sqs receive refused",
            }
        },
        "ReceiveMessage",
    )

    def receive_message(**kwargs):
        events.append(("receive", kwargs.get("QueueUrl")))
        n = sum(1 for kind, _ in events if kind == "receive")
        if n == 1:
            raise receive_error
        if n == 2:
            return {}
        if n == 3:
            raise KeyboardInterrupt()
        raise SystemExit("worker loop did not stop")

    def sleep(seconds):
        events.append(("sleep", seconds))
        if sum(1 for kind, _ in events if kind == "sleep") > 1:
            raise SystemExit("worker loop did not stop")

    sqs.receive_message.side_effect = receive_message

    with patch("worker_app.worker._configure_tracing") as tracing, patch(
        "worker_app.worker.get_sqs_client", return_value=sqs
    ) as get_sqs, patch("worker_app.worker.time.sleep", side_effect=sleep), patch(
        "worker_app.worker.handle_message"
    ) as handle:
        with caplog.at_level(logging.INFO, logger="worker_app.worker"):
            try:
                main()
            except KeyboardInterrupt:
                pytest.fail("KeyboardInterrupt escaped the worker loop")
            except SystemExit as exc:
                pytest.fail(f"worker loop did not stop: {exc}")

    tracing.assert_called_once_with()
    get_sqs.assert_called_once_with()
    handle.assert_not_called()
    sqs.delete_message.assert_not_called()
    assert events == [
        ("receive", queue_url),
        ("sleep", 2),
        ("receive", queue_url),
        ("receive", queue_url),
    ]
    records = _worker_log_records(caplog)
    assert [(r.levelno, r.message) for r in records] == [
        (logging.INFO, f"worker_started queue={queue_url}"),
        (logging.ERROR, "worker_loop_error"),
        (logging.INFO, "worker_stopping"),
    ]
    assert records[0].exc_info is None
    assert isinstance(records[1].exc_info[1], ClientError)
    assert records[1].exc_info[1].operation_name == "ReceiveMessage"
    assert records[1].exc_info[1].response["Error"]["Code"] == "ServiceUnavailable"
    assert records[1].exc_info[1].response["Error"]["Message"] == "sqs receive refused"
    assert records[2].exc_info is None
