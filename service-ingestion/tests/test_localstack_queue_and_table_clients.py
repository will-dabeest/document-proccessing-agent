"""Ingestion queue and status-table clients on the default LocalStack path.

S3 endpoint selection is covered when LocalStack is on and when it is off.
SQS and DynamoDB were only asserted with LocalStack disabled, where the
endpoint must be cleared. Publishing jobs and listing documents use these
other two clients, and each function copies the endpoint conditional, so a
LocalStack URL or credential dropped from one of them would not fail the
S3 tests.
"""

from unittest.mock import patch

from app import aws_clients


def test_get_sqs_client_uses_localstack_endpoint_and_credentials():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", True), patch.object(
        aws_clients.settings, "aws_endpoint", "http://ingest-queue-local:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "ingest-queue-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "ingest-queue-secret"
    ), patch.object(aws_clients.settings, "aws_region", "us-west-1"), patch(
        "app.aws_clients.boto3.client", return_value=fake
    ) as client:
        out = aws_clients.get_sqs_client()

    assert out is fake
    assert client.call_args.args == ("sqs",)
    kwargs = client.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://ingest-queue-local:4566"
    assert kwargs["aws_access_key_id"] == "ingest-queue-key"
    assert kwargs["aws_secret_access_key"] == "ingest-queue-secret"
    assert kwargs["region_name"] == "us-west-1"


def test_get_dynamodb_resource_uses_localstack_endpoint_and_credentials():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", True), patch.object(
        aws_clients.settings, "aws_endpoint", "http://ingest-table-local:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "ingest-table-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "ingest-table-secret"
    ), patch.object(aws_clients.settings, "aws_region", "ca-central-1"), patch(
        "app.aws_clients.boto3.resource", return_value=fake
    ) as resource:
        out = aws_clients.get_dynamodb_resource()

    assert out is fake
    assert resource.call_args.args == ("dynamodb",)
    kwargs = resource.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://ingest-table-local:4566"
    assert kwargs["aws_access_key_id"] == "ingest-table-key"
    assert kwargs["aws_secret_access_key"] == "ingest-table-secret"
    assert kwargs["region_name"] == "ca-central-1"
