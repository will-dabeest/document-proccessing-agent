import json
import logging
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.publisher import publish_job, publish_job_safe
from shared.job_schema import JobMessage


@pytest.fixture
def sqs_queue_url(monkeypatch):
    with mock_aws():
        import app.config as cfg

        client = boto3.client("sqs", region_name=cfg.settings.aws_region)
        url = client.create_queue(QueueName="pytest-publisher-jobs")["QueueUrl"]
        monkeypatch.setattr(cfg.settings, "use_localstack", False)
        monkeypatch.setattr(cfg.settings, "queue_url", url)
        yield url, client


def test_publish_job_sends_json_body(sqs_queue_url):
    url, client = sqs_queue_url
    job = JobMessage(
        s3_key="doc.txt",
        idempotency_key="id-100",
        uploaded_at="2024-01-01T00:00:00+00:00",
    )
    publish_job(job)

    resp = client.receive_message(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
    )
    body = json.loads(resp["Messages"][0]["Body"])
    assert body == job.to_json_dict()
    attrs = resp["Messages"][0].get("MessageAttributes") or {}
    if "traceparent" in attrs:
        assert attrs["traceparent"]["DataType"] == "String"


def test_publish_job_includes_traceparent_when_carrier_provided(sqs_queue_url):
    url, client = sqs_queue_url
    job = JobMessage(s3_key="a.txt", idempotency_key="id-200", uploaded_at="2024-01-02T00:00:00+00:00")
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    publish_job(job, trace_carrier={"traceparent": tp})

    resp = client.receive_message(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
    )
    attrs = resp["Messages"][0].get("MessageAttributes") or {}
    assert attrs["traceparent"]["StringValue"] == tp
    assert attrs["traceparent"]["DataType"] == "String"


def test_publish_job_drops_tracestate_from_message_attributes(sqs_queue_url):
    """Cross-service hop only forwards traceparent today; tracestate is dropped."""
    url, client = sqs_queue_url
    job = JobMessage(s3_key="ts.txt", idempotency_key="id-210", uploaded_at="2024-01-02T00:00:00+00:00")
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    publish_job(
        job,
        trace_carrier={"traceparent": tp, "tracestate": "vendor=alpha,other=1"},
    )

    resp = client.receive_message(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
    )
    attrs = resp["Messages"][0].get("MessageAttributes") or {}
    assert attrs["traceparent"]["StringValue"] == tp
    assert "tracestate" not in attrs


def test_publish_job_injects_live_otel_context_when_carrier_omitted(sqs_queue_url):
    """Production upload/import call publish_job without an explicit carrier."""
    url, client = sqs_queue_url
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("pytest-publisher")
    job = JobMessage(s3_key="live.txt", idempotency_key="id-220", uploaded_at="2024-01-02T00:00:00+00:00")

    with tracer.start_as_current_span("upload-publish"):
        expected: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(expected)
        assert "traceparent" in expected
        publish_job(job)

    resp = client.receive_message(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
    )
    attrs = resp["Messages"][0].get("MessageAttributes") or {}
    assert attrs["traceparent"]["StringValue"] == expected["traceparent"]


def test_publish_job_safe_propagates_send_failure():
    job = JobMessage(s3_key="x.txt", idempotency_key="id-300", uploaded_at="2024-01-03T00:00:00+00:00")
    mock_sqs = MagicMock()
    mock_sqs.send_message.side_effect = RuntimeError("sqs unavailable")
    with patch("app.publisher.get_sqs_client", return_value=mock_sqs):
        with pytest.raises(RuntimeError, match="sqs unavailable"):
            publish_job_safe(job, filename="x.txt")


def test_publish_job_safe_logs_err_sqs_publish_failed_with_filename(caplog):
    job = JobMessage(s3_key="ops.txt", idempotency_key="id-310", uploaded_at="2024-01-03T00:00:00+00:00")
    mock_sqs = MagicMock()
    mock_sqs.send_message.side_effect = RuntimeError("sqs unavailable")
    with patch("app.publisher.get_sqs_client", return_value=mock_sqs):
        with caplog.at_level(logging.ERROR, logger="app.publisher"):
            with pytest.raises(RuntimeError, match="sqs unavailable"):
                publish_job_safe(job, filename="ops.txt")

    assert any(rec.message == "ERR_SQS_PUBLISH_FAILED" for rec in caplog.records)
    match = next(rec for rec in caplog.records if rec.message == "ERR_SQS_PUBLISH_FAILED")
    assert match.uploaded_filename == "ops.txt"
    assert "sqs unavailable" in match.error
