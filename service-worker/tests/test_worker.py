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


def _worker_process_span_harness(trace_id_hex: str, parent_span_hex: str):
    """In-memory tracer whose finished spans stay local to the test."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-worker-span-parent")
    return exporter, provider, tracer, int(trace_id_hex, 16), int(parent_span_hex, 16)


def _assert_remote_parent(span, expected_trace_id: int, expected_parent_span_id: int):
    assert span.name == "worker-process"
    assert span.context.trace_id == expected_trace_id
    assert span.context.is_remote is False
    assert span.context.span_id != expected_parent_span_id
    parent = span.parent
    assert parent is not None
    assert parent.is_remote is True
    assert parent.trace_id == expected_trace_id
    assert parent.span_id == expected_parent_span_id
    assert parent.trace_flags.sampled is True


def _assert_claim_ran_in_child(
    observed, expected_trace_id: int, expected_parent_span_id: int
):
    current = observed["span"]
    assert current.is_valid
    assert current.is_remote is False
    assert current.trace_id == expected_trace_id
    assert current.span_id != expected_parent_span_id


def test_handle_message_duplicate_done_span_keeps_remote_parent():
    """An already-completed job still records worker-process under the SQS parent."""
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    from worker_app.config import settings

    trace_id_hex = "d00e5a0000c0ffee11aabbccddeeff01"
    parent_span_hex = "0d0e5a0011223344"
    traceparent = f"00-{trace_id_hex}-{parent_span_hex}-01"
    exporter, provider, tracer, expected_trace_id, expected_parent_span_id = (
        _worker_process_span_harness(trace_id_hex, parent_span_hex)
    )
    body = json.dumps(
        {
            "s3_key": "reports/span-done.txt",
            "idempotency_key": "job-span-done",
            "uploaded_at": "2024-06-14T00:00:00+00:00",
        }
    )
    message = {
        "Body": body,
        "MessageAttributes": {
            "traceparent": {"StringValue": traceparent, "DataType": "String"},
        },
    }
    observed = {}

    def claim_done(table, job_id):
        observed["span"] = otel_trace.get_current_span().get_span_context()
        observed["table"] = table
        observed["job_id"] = job_id
        return "duplicate_done"

    root = otel_context.Context()
    token = otel_context.attach(root)
    try:
        with patch("worker_app.worker.trace.get_tracer", return_value=tracer), patch(
            "worker_app.worker.get_dynamodb_resource"
        ) as gr, patch(
            "worker_app.worker.try_claim_job", side_effect=claim_done
        ), patch("worker_app.worker.process_job_body") as proc:
            table = MagicMock()
            gr.return_value.Table.return_value = table
            handle_message(message)
            proc.assert_not_called()
            gr.return_value.Table.assert_called_once_with(settings.dynamodb_table)
            leftover = otel_trace.get_current_span().get_span_context()
    finally:
        otel_context.detach(token)
        provider.shutdown()

    assert observed["job_id"] == "job-span-done"
    assert observed["table"] is table
    _assert_claim_ran_in_child(observed, expected_trace_id, expected_parent_span_id)
    assert leftover.is_valid is False
    assert leftover.trace_id == 0

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    _assert_remote_parent(spans[0], expected_trace_id, expected_parent_span_id)


def test_handle_message_duplicate_inflight_span_keeps_remote_parent():
    """An in-flight duplicate raises after worker-process is parented and detached."""
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    from worker_app.config import settings

    trace_id_hex = "1f11607e0000c0ffee22aabbccddeeff"
    parent_span_hex = "1f11607e55667788"
    traceparent = f"00-{trace_id_hex}-{parent_span_hex}-01"
    exporter, provider, tracer, expected_trace_id, expected_parent_span_id = (
        _worker_process_span_harness(trace_id_hex, parent_span_hex)
    )
    body = json.dumps(
        {
            "s3_key": "reports/span-inflight.txt",
            "idempotency_key": "job-span-inflight",
            "uploaded_at": "2024-06-15T00:00:00+00:00",
        }
    )
    message = {
        "Body": body,
        "MessageAttributes": {
            "traceparent": {"StringValue": traceparent, "DataType": "String"},
        },
    }
    observed = {}

    def claim_inflight(table, job_id):
        observed["span"] = otel_trace.get_current_span().get_span_context()
        observed["table"] = table
        observed["job_id"] = job_id
        return "duplicate_inflight"

    root = otel_context.Context()
    token = otel_context.attach(root)
    try:
        with patch("worker_app.worker.trace.get_tracer", return_value=tracer), patch(
            "worker_app.worker.get_dynamodb_resource"
        ) as gr, patch(
            "worker_app.worker.try_claim_job", side_effect=claim_inflight
        ), patch("worker_app.worker.process_job_body") as proc:
            table = MagicMock()
            gr.return_value.Table.return_value = table
            with pytest.raises(RuntimeError, match="duplicate_inflight_retry"):
                handle_message(message)
            proc.assert_not_called()
            gr.return_value.Table.assert_called_once_with(settings.dynamodb_table)
            leftover = otel_trace.get_current_span().get_span_context()
    finally:
        otel_context.detach(token)
        provider.shutdown()

    assert observed["job_id"] == "job-span-inflight"
    assert observed["table"] is table
    _assert_claim_ran_in_child(observed, expected_trace_id, expected_parent_span_id)
    assert leftover.is_valid is False
    assert leftover.trace_id == 0

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    _assert_remote_parent(spans[0], expected_trace_id, expected_parent_span_id)


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
