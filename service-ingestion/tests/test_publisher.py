import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

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


def test_publish_job_whitespace_traceparent_is_attached_verbatim(sqs_queue_url):
    url, client = sqs_queue_url
    ambient = "00-9f8e7d6c5b4a39281706f5e4d3c2b1a0-0f1e2d3c4b5a6978-01"
    cases = (
        ("   ", "id-ws-tp-spaces", "2024-06-11T00:00:00+00:00", "notes/ws-trace-spaces.txt"),
        ("\n", "id-ws-tp-newline", "2024-06-12T00:00:00+00:00", "notes/ws-trace-newline.txt"),
    )
    for raw, job_id, uploaded_at, s3_key in cases:
        job = JobMessage(
            s3_key=s3_key,
            idempotency_key=job_id,
            uploaded_at=uploaded_at,
        )
        with patch(
            "app.publisher._inject_trace_carrier",
            return_value={"traceparent": ambient},
        ) as inject:
            publish_job(job, trace_carrier={"traceparent": raw})

        inject.assert_not_called()
        resp = client.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=1,
            MessageAttributeNames=["All"],
        )
        message = resp["Messages"][0]
        assert json.loads(message["Body"]) == job.to_json_dict()
        attrs = message.get("MessageAttributes") or {}
        assert attrs["traceparent"]["StringValue"] == raw
        assert attrs["traceparent"]["DataType"] == "String"
        assert ambient not in message["Body"]


def test_publish_job_safe_propagates_send_failure():
    job = JobMessage(s3_key="x.txt", idempotency_key="id-300", uploaded_at="2024-01-03T00:00:00+00:00")
    mock_sqs = MagicMock()
    mock_sqs.send_message.side_effect = RuntimeError("sqs unavailable")
    with patch("app.publisher.get_sqs_client", return_value=mock_sqs):
        with pytest.raises(RuntimeError, match="sqs unavailable"):
            publish_job_safe(job, filename="x.txt")
