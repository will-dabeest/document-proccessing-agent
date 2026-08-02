import json
from unittest.mock import MagicMock, patch

import pytest

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


def _stub_tracer_span():
    """Avoid nested OTEL attach/detach from start_as_current_span during token tests."""
    span_cm = MagicMock()
    span_cm.__enter__ = MagicMock(return_value=MagicMock())
    span_cm.__exit__ = MagicMock(return_value=False)
    tracer = MagicMock()
    tracer.start_as_current_span.return_value = span_cm
    return tracer


def test_handle_message_detaches_otel_context_on_success():
    body = json.dumps(
        {
            "s3_key": "g.txt",
            "idempotency_key": "jid-otel-ok",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    token = object()
    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.try_claim_job", return_value="claimed"
    ), patch("worker_app.worker.process_job_body"), patch(
        "worker_app.worker.trace.get_tracer", return_value=_stub_tracer_span()
    ), patch(
        "worker_app.worker.otel_context.attach", return_value=token
    ) as attach, patch(
        "worker_app.worker.otel_context.detach"
    ) as detach:
        gr.return_value.Table.return_value = MagicMock()
        handle_message({"Body": body})

    attach.assert_called_once()
    detach.assert_called_once_with(token)


def test_handle_message_detaches_otel_context_on_inflight_raise():
    body = json.dumps(
        {
            "s3_key": "g.txt",
            "idempotency_key": "jid-otel-inflight",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    token = object()
    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.try_claim_job", return_value="duplicate_inflight"
    ), patch(
        "worker_app.worker.trace.get_tracer", return_value=_stub_tracer_span()
    ), patch(
        "worker_app.worker.otel_context.attach", return_value=token
    ), patch(
        "worker_app.worker.otel_context.detach"
    ) as detach:
        gr.return_value.Table.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="duplicate_inflight_retry"):
            handle_message({"Body": body})

    detach.assert_called_once_with(token)


def test_handle_message_detaches_otel_context_when_process_fails():
    body = json.dumps(
        {
            "s3_key": "g.txt",
            "idempotency_key": "jid-otel-fail",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )
    token = object()
    with patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
        "worker_app.worker.try_claim_job", return_value="claimed"
    ), patch(
        "worker_app.worker.process_job_body", side_effect=RuntimeError("s3 boom")
    ), patch(
        "worker_app.worker.trace.get_tracer", return_value=_stub_tracer_span()
    ), patch(
        "worker_app.worker.otel_context.attach", return_value=token
    ), patch(
        "worker_app.worker.otel_context.detach"
    ) as detach:
        gr.return_value.Table.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="s3 boom"):
            handle_message({"Body": body})

    detach.assert_called_once_with(token)


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
