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


def test_main_polls_after_tracing_exporter_failure(caplog):
    queue_url = "http://localhost:4566/000000000000/coverage-tracing-init"
    exporter_error = RuntimeError("otlp exporter refused")
    sqs = MagicMock()
    sqs.receive_message.side_effect = [
        {},
        KeyboardInterrupt(),
        SystemExit("coverage-tracing-init did not stop"),
    ]

    with caplog.at_level(logging.INFO, logger="worker_app.worker"):
        with (
            patch(
                "worker_app.worker.OTLPSpanExporter",
                side_effect=exporter_error,
            ) as exporter,
            patch("worker_app.worker.trace.set_tracer_provider") as set_provider,
            patch("worker_app.worker.get_sqs_client", return_value=sqs) as get_sqs,
            patch("worker_app.worker.handle_message") as handle,
            patch("worker_app.worker.time.sleep") as sleep,
            patch("worker_app.worker.settings") as runtime_settings,
        ):
            runtime_settings.otlp_endpoint = "worker-otlp-fail.internal:4317"
            runtime_settings.queue_url = queue_url
            failure = None
            try:
                main()
            except (KeyboardInterrupt, SystemExit) as exc:
                failure = f"KeyboardInterrupt escaped the worker loop: {exc!r}"
            except Exception as exc:
                failure = (
                    "tracing init failure aborted the worker before polling: "
                    f"{exc!r}"
                )
            if failure is not None:
                pytest.fail(failure)

    exporter.assert_called_once_with(
        endpoint="worker-otlp-fail.internal:4317",
        insecure=True,
    )
    set_provider.assert_not_called()
    get_sqs.assert_called_once_with()
    sleep.assert_not_called()
    handle.assert_not_called()
    sqs.delete_message.assert_not_called()
    assert sqs.receive_message.call_count == 2
    sqs.receive_message.assert_called_with(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        MessageAttributeNames=["All"],
    )

    worker_logs = [
        record for record in caplog.records if record.name == "worker_app.worker"
    ]
    assert [(record.levelno, record.message) for record in worker_logs] == [
        (logging.WARNING, "tracing_init_failed"),
        (logging.INFO, f"worker_started queue={queue_url}"),
        (logging.INFO, "worker_stopping"),
    ]
    assert worker_logs[0].exc_info is not None
    assert worker_logs[0].exc_info[1] is exporter_error
    assert worker_logs[1].exc_info is None
    assert worker_logs[2].exc_info is None
    assert not any(record.message == "worker_loop_error" for record in caplog.records)
