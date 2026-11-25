"""Worker object downloads must leave LocalStack when use_localstack is off."""

from unittest.mock import patch

from worker_app.aws_clients import get_s3_client

_ENDPOINT = "http://worker-s3-off:4566"
_KEY_ID = "worker-s3-off-key"
_SECRET = "worker-s3-off-secret"
_REGION = "eu-central-1"


def test_worker_s3_client_clears_endpoint_off_localstack():
    """Real AWS downloads ignore the LocalStack URL and still send credentials.

    ``download_object_bytes`` uses this client for every object read.
    """
    fake = object()
    with patch("worker_app.aws_clients.settings") as aws_settings, patch(
        "worker_app.aws_clients.boto3.client", return_value=fake
    ) as boto_client:
        aws_settings.use_localstack = False
        aws_settings.aws_endpoint = _ENDPOINT
        aws_settings.aws_access_key_id = _KEY_ID
        aws_settings.aws_secret_access_key = _SECRET
        aws_settings.aws_region = _REGION

        assert get_s3_client() is fake

    boto_client.assert_called_once_with(
        "s3",
        endpoint_url=None,
        aws_access_key_id=_KEY_ID,
        aws_secret_access_key=_SECRET,
        region_name=_REGION,
    )
