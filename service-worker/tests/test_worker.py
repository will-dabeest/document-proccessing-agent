import json
import logging
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


def test_run_once_process_failure_finishes_span_and_keeps_message(caplog):
    """A claimed job that fails in processing stays queued and keeps its trace parent.

    The worker-process span is a local child of the sampled SQS traceparent, the
    context is detached after the error leaves handle_message, and run_once does
    not delete the message.
    """
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode

    from worker_app.config import settings

    trace_id_hex = "b10c5fa110000c0ffee44aabbccddeef"
    parent_span_hex = "b10c5fa155667799"
    traceparent = f"00-{trace_id_hex}-{parent_span_hex}-01"
    expected_trace_id = int(trace_id_hex, 16)
    expected_parent_span_id = int(parent_span_hex, 16)
    job_id = "job-span-process-fail"
    s3_key = "reports/span-process-fail.txt"
    queue_url = "http://localhost:4566/000000000000/coverage-process-fail"
    receipt = "rh-process-fail-keep"
    message_id = "sqs-msg-process-fail-keep"
    envelope_marker = "DOC_MARKER_process_fail_envelope"
    error_text = "process_body_refused"

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-worker-process-fail")

    body = json.dumps(
        {
            "s3_key": s3_key,
            "idempotency_key": job_id,
            "uploaded_at": "2024-06-16T00:00:00+00:00",
        }
    )
    message = {
        "MessageId": message_id,
        "ReceiptHandle": receipt,
        "Body": body,
        "MessageAttributes": {
            "traceparent": {"StringValue": traceparent, "DataType": "String"},
            "document_name": {"StringValue": envelope_marker, "DataType": "String"},
        },
    }
    observed = {}

    def fail_process(called_job_id, called_key, called_table):
        observed["span"] = otel_trace.get_current_span().get_span_context()
        observed["job_id"] = called_job_id
        observed["s3_key"] = called_key
        observed["table"] = called_table
        raise RuntimeError(error_text)

    sqs = MagicMock()
    sqs.receive_message.return_value = {"Messages": [message]}
    root = otel_context.Context()
    token = otel_context.attach(root)
    try:
        with caplog.at_level(logging.ERROR), patch(
            "worker_app.worker.trace.get_tracer", return_value=tracer
        ), patch("worker_app.worker.get_dynamodb_resource") as gr, patch(
            "worker_app.worker.try_claim_job", return_value="claimed"
        ) as claim, patch(
            "worker_app.worker.process_job_body", side_effect=fail_process
        ):
            table = MagicMock()
            gr.return_value.Table.return_value = table
            assert run_once(sqs, queue_url) is True
            gr.return_value.Table.assert_called_once_with(settings.dynamodb_table)
            claim.assert_called_once_with(table, job_id)
            leftover = otel_trace.get_current_span().get_span_context()
    finally:
        otel_context.detach(token)
        provider.shutdown()

    assert observed["job_id"] == job_id
    assert observed["s3_key"] == s3_key
    assert observed["table"] is table
    current = observed["span"]
    assert current.is_valid
    assert current.is_remote is False
    assert current.trace_id == expected_trace_id
    assert current.span_id != expected_parent_span_id

    assert leftover.is_valid is False
    assert leftover.trace_id == 0
    sqs.delete_message.assert_not_called()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "worker-process"
    assert span.context.trace_id == expected_trace_id
    assert span.context.is_remote is False
    assert span.context.span_id != expected_parent_span_id
    assert span.status.status_code == StatusCode.ERROR
    parent = span.parent
    assert parent is not None
    assert parent.is_remote is True
    assert parent.trace_id == expected_trace_id
    assert parent.span_id == expected_parent_span_id
    assert parent.trace_flags.sampled is True
    exception_events = [event for event in span.events if event.name == "exception"]
    assert len(exception_events) == 1
    assert exception_events[0].attributes["exception.type"] == "RuntimeError"
    assert exception_events[0].attributes["exception.message"] == error_text

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "worker_app.worker"
    assert errors[0].getMessage().startswith("message_processing_failed:")
    assert error_text in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert isinstance(errors[0].exc_info[1], RuntimeError)
    assert str(errors[0].exc_info[1]) == error_text
    rendered = [errors[0].getMessage()]
    if errors[0].exc_info:
        import traceback

        rendered.append("".join(traceback.format_exception(*errors[0].exc_info)))
    blob = "\n".join(rendered)
    assert receipt not in blob
    assert message_id not in blob
    assert queue_url not in blob
    assert envelope_marker not in blob
    assert "worker_loop_error" not in blob


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
