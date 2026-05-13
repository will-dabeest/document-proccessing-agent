import boto3

from app.config import settings


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.aws_endpoint if settings.use_localstack else None,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


def get_sqs_client():
    return boto3.client(
        "sqs",
        endpoint_url=settings.aws_endpoint if settings.use_localstack else None,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


def get_dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        endpoint_url=settings.aws_endpoint if settings.use_localstack else None,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
