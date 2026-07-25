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


def test_publish_job_omits_traceparent_when_carrier_empty(sqs_queue_url):
    """Empty/missing traceparent must not publish a blank SQS message attribute."""
    url, client = sqs_queue_url
    job = JobMessage(s3_key="b.txt", idempotency_key="id-201", uploaded_at="2024-01-02T00:00:00+00:00")
    publish_job(job, trace_carrier={})

    resp = client.receive_message(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
    )
    body = json.loads(resp["Messages"][0]["Body"])
    assert body == job.to_json_dict()
    attrs = resp["Messages"][0].get("MessageAttributes") or {}
    assert "traceparent" not in attrs


def test_publish_job_omits_traceparent_when_value_blank(sqs_queue_url):
    url, client = sqs_queue_url
    job = JobMessage(s3_key="c.txt", idempotency_key="id-202", uploaded_at="2024-01-02T00:00:00+00:00")
    publish_job(job, trace_carrier={"traceparent": ""})

    resp = client.receive_message(
        QueueUrl=url,
        MaxNumberOfMessages=1,
        MessageAttributeNames=["All"],
    )
    attrs = resp["Messages"][0].get("MessageAttributes") or {}
    assert "traceparent" not in attrs


def test_publish_job_safe_propagates_send_failure():
    job = JobMessage(s3_key="x.txt", idempotency_key="id-300", uploaded_at="2024-01-03T00:00:00+00:00")
    mock_sqs = MagicMock()
    mock_sqs.send_message.side_effect = RuntimeError("sqs unavailable")
    with patch("app.publisher.get_sqs_client", return_value=mock_sqs):
        with pytest.raises(RuntimeError, match="sqs unavailable"):
            publish_job_safe(job, filename="x.txt")
