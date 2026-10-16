"""Worker trace span and failure log are the on-call signal for queue processing."""

import json
import logging
from unittest.mock import MagicMock, patch

from worker_app.worker import handle_message, run_once


def _job_body() -> str:
    return json.dumps(
        {
            "s3_key": "notes.txt",
            "idempotency_key": "job-visibility",
            "uploaded_at": "2024-01-01T00:00:00+00:00",
        }
    )


def test_handle_message_opens_worker_process_span_before_claim():
    order: list[object] = []
    span_cm = MagicMock()
    span_cm.__enter__.return_value = MagicMock()
    span_cm.__exit__.return_value = False
    tracer = MagicMock()

    def start(name):
        order.append(("span", name))
        return span_cm

    tracer.start_as_current_span.side_effect = start

    def claim(*_args, **_kwargs):
        order.append("claim")
        return "claimed"

    def process(*_args, **_kwargs):
        order.append("process")

    with patch("worker_app.worker.get_dynamodb_resource") as resource, patch(
        "worker_app.worker.try_claim_job", side_effect=claim
    ), patch("worker_app.worker.process_job_body", side_effect=process), patch(
        "worker_app.worker.trace.get_tracer", return_value=tracer
    ):
        resource.return_value.Table.return_value = MagicMock()
        handle_message({"Body": _job_body()})

    assert order == [("span", "worker-process"), "claim", "process"]
    tracer.start_as_current_span.assert_called_once_with("worker-process")


def test_run_once_logs_processing_failure_and_leaves_message(caplog):
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [{"ReceiptHandle": "rh-visibility", "Body": _job_body()}],
    }
    with caplog.at_level(logging.ERROR, logger="worker_app.worker"), patch(
        "worker_app.worker.handle_message",
        side_effect=RuntimeError("queue-visibility-marker"),
    ):
        assert run_once(sqs, "http://example/queue") is True

    assert "message_processing_failed: queue-visibility-marker" in caplog.text
    sqs.delete_message.assert_not_called()
