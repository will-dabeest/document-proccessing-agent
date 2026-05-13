from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry.propagate import inject

from app.aws_clients import get_sqs_client
from app.config import settings
from shared.job_schema import JobMessage

logger = logging.getLogger(__name__)


def _inject_trace_carrier() -> dict[str, str]:
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def publish_job(
    job: JobMessage,
    *,
    trace_carrier: dict[str, str] | None = None,
) -> None:
    sqs = get_sqs_client()
    body = json.dumps(job.to_json_dict())
    message_attributes: dict[str, dict[str, str]] = {}
    carrier = trace_carrier if trace_carrier is not None else _inject_trace_carrier()
    tp = carrier.get("traceparent")
    if tp:
        message_attributes["traceparent"] = {"StringValue": tp, "DataType": "String"}

    sqs.send_message(
        QueueUrl=settings.queue_url,
        MessageBody=body,
        MessageAttributes=message_attributes,
    )


def publish_job_safe(job: JobMessage, *, filename: str) -> None:
    try:
        publish_job(job)
    except Exception as e:
        logger.error(
            "ERR_SQS_PUBLISH_FAILED",
            extra={"uploaded_filename": filename, "error": str(e)},
        )
        raise
