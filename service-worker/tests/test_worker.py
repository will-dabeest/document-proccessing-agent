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


def test_handle_message_traceparent_is_remote_parent_of_worker_process():
    """The SQS traceparent is the remote parent of the worker-process span."""
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    trace_id_hex = "c0ffee00d1e5c0de0badf00d12345678"
    parent_span_hex = "1122334455667788"
    traceparent = f"00-{trace_id_hex}-{parent_span_hex}-01"
    expected_trace_id = int(trace_id_hex, 16)
    expected_parent_span_id = int(parent_span_hex, 16)

    body = json.dumps(
        {
            "s3_key": "reports/span-parent.txt",
            "idempotency_key": "job-span-parent",
            "uploaded_at": "2024-06-13T00:00:00+00:00",
        }
    )
    message = {
        "Body": body,
        "MessageAttributes": {
            "traceparent": {"StringValue": traceparent, "DataType": "String"},
        },
    }

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-worker-span-parent")

    def assert_work_runs_in_child_span(*_args, **_kwargs):
        current = otel_trace.get_current_span().get_span_context()
        assert current.is_valid
        assert current.is_remote is False
        assert current.trace_id == expected_trace_id
        assert current.span_id != expected_parent_span_id

    root = otel_context.Context()
    token = otel_context.attach(root)
    try:
        with patch("worker_app.worker.trace.get_tracer", return_value=tracer), patch(
            "worker_app.worker.get_dynamodb_resource"
        ) as gr, patch(
            "worker_app.worker.try_claim_job", return_value="claimed"
        ), patch(
            "worker_app.worker.process_job_body",
            side_effect=assert_work_runs_in_child_span,
        ) as proc:
            gr.return_value.Table.return_value = MagicMock()
            handle_message(message)
            proc.assert_called_once_with(
                "job-span-parent",
                "reports/span-parent.txt",
                gr.return_value.Table.return_value,
            )
            leftover = otel_trace.get_current_span().get_span_context()
    finally:
        otel_context.detach(token)
        provider.shutdown()

    assert leftover.is_valid is False
    assert leftover.trace_id == 0

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
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
