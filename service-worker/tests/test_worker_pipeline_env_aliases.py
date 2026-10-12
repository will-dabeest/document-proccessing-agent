"""Compose names that route the worker queue, status table, bucket, and model.

Ingestion alias coverage does not import this Settings class. Values differ
from the code defaults so a dropped alias fails.
"""

from worker_app.config import Settings


def test_worker_settings_read_pipeline_env_aliases(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "false")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("BUCKET_NAME", "prod-docs")
    monkeypatch.delenv("bucket_name", raising=False)
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example/jobs")
    monkeypatch.setenv("DYNAMODB_TABLE", "ProcessLogProd")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3:8b")
    monkeypatch.setenv("OTLP_ENDPOINT", "jaeger:4317")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    settings = Settings(_env_file=None)

    assert settings.use_localstack is False
    assert settings.aws_region == "eu-west-1"
    assert settings.bucket_name == "prod-docs"
    assert settings.queue_url == "https://sqs.example/jobs"
    assert settings.dynamodb_table == "ProcessLogProd"
    assert settings.ollama_base_url == "http://ollama:11434"
    assert settings.ollama_model == "llama3:8b"
    assert settings.otlp_endpoint == "jaeger:4317"
