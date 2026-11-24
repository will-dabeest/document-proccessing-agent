"""Worker SQS retries stay on when LocalStack is the configured endpoint.

The off-LocalStack client test locks retries together with a cleared
endpoint. The default worker path keeps ``use_localstack`` enabled, and
that is the client ``run_once`` uses to poll. Retry policy must still be
the standard five-attempt config on that path, aimed at the configured
LocalStack endpoint and credentials.
"""

from unittest.mock import patch

from botocore.config import Config

from worker_app import aws_clients


def test_worker_sqs_client_keeps_retry_policy_on_localstack():
    fake = object()
    with patch.object(aws_clients.settings, "use_localstack", True), patch.object(
        aws_clients.settings, "aws_endpoint", "http://worker-queue-local:4566"
    ), patch.object(
        aws_clients.settings, "aws_access_key_id", "worker-queue-key"
    ), patch.object(
        aws_clients.settings, "aws_secret_access_key", "worker-queue-secret"
    ), patch.object(aws_clients.settings, "aws_region", "ap-southeast-2"), patch(
        "worker_app.aws_clients.boto3.client", return_value=fake
    ) as client:
        out = aws_clients.get_sqs_client()

    assert out is fake
    assert client.call_args.args == ("sqs",)
    kwargs = client.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://worker-queue-local:4566"
    assert kwargs["aws_access_key_id"] == "worker-queue-key"
    assert kwargs["aws_secret_access_key"] == "worker-queue-secret"
    assert kwargs["region_name"] == "ap-southeast-2"
    config = kwargs["config"]
    assert isinstance(config, Config)
    assert config.retries["max_attempts"] == 5
    assert config.retries["mode"] == "standard"
