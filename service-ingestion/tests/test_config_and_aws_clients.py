"""Config alias parsing and AWS client endpoint/credential wiring."""

from unittest.mock import patch

from app import aws_clients
from app.config import Settings


def test_settings_accept_endpoint_and_bucket_aliases(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT", "http://alias-endpoint:4566")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("bucket_name", "alias-bucket")
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    monkeypatch.setenv("URL_IMPORT_MAX_BYTES", "2048")
    monkeypatch.setenv("URL_IMPORT_ENABLED", "false")

    settings = Settings(_env_file=None)
    assert settings.aws_endpoint == "http://alias-endpoint:4566"
    assert settings.bucket_name == "alias-bucket"
    assert settings.url_import_max_bytes == 2048
    assert settings.url_import_enabled is False


def test_get_s3_client_uses_localstack_endpoint_when_enabled():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", True), patch.object(
        aws_clients.settings, "aws_endpoint", "http://localstack:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "test-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "test-secret"
    ), patch.object(aws_clients.settings, "aws_region", "eu-west-1"), patch(
        "app.aws_clients.boto3.client", return_value=fake
    ) as client:
        out = aws_clients.get_s3_client()

    assert out is fake
    kwargs = client.call_args.kwargs
    assert client.call_args.args[0] == "s3"
    assert kwargs["endpoint_url"] == "http://localstack:4566"
    assert kwargs["aws_access_key_id"] == "test-key"
    assert kwargs["aws_secret_access_key"] == "test-secret"
    assert kwargs["region_name"] == "eu-west-1"


def test_get_sqs_and_dynamodb_clear_endpoint_when_not_localstack():
    with patch.object(aws_clients.settings, "use_localstack", False), patch.object(
        aws_clients.settings, "aws_endpoint", "http://should-not-be-used:4566"
    ), patch("app.aws_clients.boto3.client") as client, patch(
        "app.aws_clients.boto3.resource"
    ) as resource:
        aws_clients.get_sqs_client()
        aws_clients.get_dynamodb_resource()

    assert client.call_args.kwargs["endpoint_url"] is None
    assert resource.call_args.kwargs["endpoint_url"] is None
    assert client.call_args.args[0] == "sqs"
    assert resource.call_args.args[0] == "dynamodb"
