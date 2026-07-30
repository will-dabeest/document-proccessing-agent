"""Worker config aliases and AWS client wiring (including SQS retry policy)."""

from unittest.mock import patch

from worker_app import aws_clients
from worker_app.config import Settings


def test_worker_settings_accept_ingestion_and_otlp_aliases(monkeypatch):
    monkeypatch.setenv("INGESTION_BASE_URL", "http://ingestion:8000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://aws-alias:4566")
    monkeypatch.delenv("AWS_ENDPOINT", raising=False)

    settings = Settings(_env_file=None)
    assert settings.ingestion_base_url == "http://ingestion:8000"
    assert settings.otlp_endpoint == "http://jaeger:4317"
    assert settings.aws_endpoint == "http://aws-alias:4566"


def test_worker_sqs_client_sets_retry_config_and_clears_endpoint_off_localstack():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", False), patch.object(
        aws_clients.settings, "aws_endpoint", "http://unused:4566"
    ), patch.object(aws_clients.settings, "aws_region", "us-west-2"), patch(
        "worker_app.aws_clients.boto3.client", return_value=fake
    ) as client, patch("worker_app.aws_clients.Config") as config_cls:
        config_cls.return_value = "retry-config"
        out = aws_clients.get_sqs_client()

    assert out is fake
    config_cls.assert_called_once_with(retries={"max_attempts": 5, "mode": "standard"})
    kwargs = client.call_args.kwargs
    assert client.call_args.args[0] == "sqs"
    assert kwargs["config"] == "retry-config"
    assert kwargs["endpoint_url"] is None
    assert kwargs["region_name"] == "us-west-2"


def test_worker_s3_client_uses_localstack_endpoint_when_enabled():
    with patch.object(aws_clients.settings, "use_localstack", True), patch.object(
        aws_clients.settings, "aws_endpoint", "http://localstack:4566"
    ), patch("worker_app.aws_clients.boto3.client") as client:
        aws_clients.get_s3_client()

    assert client.call_args.kwargs["endpoint_url"] == "http://localstack:4566"
    assert client.call_args.args[0] == "s3"
