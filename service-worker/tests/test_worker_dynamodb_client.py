"""Worker DynamoDB resource wiring.

Job claims and completion writes go through ``get_dynamodb_resource``.
SQS retry policy and the S3 client are covered separately; this module
locks the status-table client so a dropped endpoint or credential does
not send ProcessLog traffic at the wrong AWS account.
"""

from unittest.mock import patch

from worker_app import aws_clients


def test_worker_dynamodb_resource_uses_localstack_endpoint_and_credentials():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", True), patch.object(
        aws_clients.settings, "aws_endpoint", "http://localstack:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "local-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "local-secret"
    ), patch.object(aws_clients.settings, "aws_region", "us-east-1"), patch(
        "worker_app.aws_clients.boto3.resource", return_value=fake
    ) as resource:
        out = aws_clients.get_dynamodb_resource()

    assert out is fake
    assert resource.call_args.args == ("dynamodb",)
    kwargs = resource.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://localstack:4566"
    assert kwargs["aws_access_key_id"] == "local-key"
    assert kwargs["aws_secret_access_key"] == "local-secret"
    assert kwargs["region_name"] == "us-east-1"


def test_worker_dynamodb_resource_clears_endpoint_when_not_localstack():
    with patch.object(aws_clients.settings, "use_localstack", False), patch.object(
        aws_clients.settings, "aws_endpoint", "http://should-not-be-used:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "prod-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "prod-secret"
    ), patch.object(aws_clients.settings, "aws_region", "eu-central-1"), patch(
        "worker_app.aws_clients.boto3.resource"
    ) as resource:
        aws_clients.get_dynamodb_resource()

    assert resource.call_args.args == ("dynamodb",)
    kwargs = resource.call_args.kwargs
    assert kwargs["endpoint_url"] is None
    assert kwargs["aws_access_key_id"] == "prod-key"
    assert kwargs["aws_secret_access_key"] == "prod-secret"
    assert kwargs["region_name"] == "eu-central-1"
