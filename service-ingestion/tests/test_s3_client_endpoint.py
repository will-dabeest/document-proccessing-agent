"""Ingestion S3 client endpoint selection when LocalStack is disabled.

``get_s3_client`` duplicates the endpoint conditional used by SQS and
DynamoDB. Uploads must not keep sending the LocalStack URL once
``use_localstack`` is false, and they must still pass the configured
credentials and region.
"""

from unittest.mock import patch

from app import aws_clients


def test_get_s3_client_clears_endpoint_when_not_localstack():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", False), patch.object(
        aws_clients.settings, "aws_endpoint", "http://should-not-be-used:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "prod-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "prod-secret"
    ), patch.object(aws_clients.settings, "aws_region", "eu-west-1"), patch(
        "app.aws_clients.boto3.client", return_value=fake
    ) as client:
        out = aws_clients.get_s3_client()

    assert out is fake
    assert client.call_args.args == ("s3",)
    kwargs = client.call_args.kwargs
    assert kwargs["endpoint_url"] is None
    assert kwargs["aws_access_key_id"] == "prod-key"
    assert kwargs["aws_secret_access_key"] == "prod-secret"
    assert kwargs["region_name"] == "eu-west-1"
