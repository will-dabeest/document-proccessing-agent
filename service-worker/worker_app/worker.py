from __future__ import annotations

import logging
import sys
import time

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from worker_app.aws_clients import get_dynamodb_resource, get_sqs_client
from worker_app.config import settings
from worker_app.processor import process_job_body, try_claim_job
from worker_app.tracing import extract_trace_from_message
from shared.job_schema import parse_job_message

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def _normalize_otlp_endpoint(raw: str) -> str:
    ep = raw.strip()
    for prefix in ("https://", "http://"):
        if ep.startswith(prefix):
            return ep[len(prefix) :]
    return ep


def _configure_tracing() -> None:
    try:
        resource = Resource.create({"service.name": "service-worker"})
        provider = TracerProvider(resource=resource)
        endpoint = _normalize_otlp_endpoint(settings.otlp_endpoint)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception:
        logger.warning("tracing_init_failed", exc_info=True)


def handle_message(message: dict) -> None:
    body_raw = message.get("Body") or "{}"
    job = parse_job_message(body_raw)
    job_id = job.idempotency_key
    s3_key = job.s3_key

    ctx = extract_trace_from_message(message)
    token = otel_context.attach(ctx)
    tracer = trace.get_tracer(__name__)
    try:
        with tracer.start_as_current_span("worker-process"):
            table = get_dynamodb_resource().Table(settings.dynamodb_table)
            claim = try_claim_job(table, job_id)
            if claim == "duplicate_done":
                return
            if claim == "duplicate_inflight":
                raise RuntimeError("duplicate_inflight_retry")
            process_job_body(job_id, s3_key, table)
    finally:
        otel_context.detach(token)


def run_once(sqs, queue_url: str) -> bool:
    resp = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        MessageAttributeNames=["All"],
    )
    messages = resp.get("Messages") or []
    if not messages:
        return False
    msg = messages[0]
    receipt = msg["ReceiptHandle"]
    try:
        handle_message(msg)
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    except Exception as e:
        logger.exception("message_processing_failed: %s", e)
    return True


def main() -> None:
    _configure_tracing()
    sqs = get_sqs_client()
    queue_url = settings.queue_url
    logger.info("worker_started queue=%s", queue_url)
    while True:
        try:
            run_once(sqs, queue_url)
        except KeyboardInterrupt:
            logger.info("worker_stopping")
            break
        except Exception:
            logger.exception("worker_loop_error")
            time.sleep(2)


if __name__ == "__main__":
    main()
