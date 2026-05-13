import boto3
from botocore.config import Config

from worker_app.config import settings


def _client_kwargs():
    ep = settings.aws_endpoint if settings.use_localstack else None
    return dict(
        endpoint_url=ep,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


def get_s3_client():
    return boto3.client("s3", **_client_kwargs())


def get_sqs_client():
    return boto3.client(
        "sqs",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        **_client_kwargs(),
    )


def get_dynamodb_resource():
    return boto3.resource("dynamodb", **_client_kwargs())
