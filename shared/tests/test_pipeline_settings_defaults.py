"""Unset process env must still point both services at the same local pipeline.

Alias tests prove non-default env values are read. They still pass if a code
default drifts, which would split the bucket, queue, table, or model when
Compose and `.env` do not set that key.
"""

import pytest

_PIPELINE_ENV = (
    "USE_LOCALSTACK",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "BUCKET_NAME",
    "bucket_name",
    "QUEUE_URL",
    "DYNAMODB_TABLE",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "OLLAMA_HTTP_TIMEOUT_SECONDS",
    "OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "INGESTION_BASE_URL",
    "LLM_MOCK_JSON",
    "CHROMA_PERSIST_DIR",
    "EMBEDDING_MODEL_NAME",
)


@pytest.fixture
def cleared_pipeline_env(monkeypatch):
    for key in _PIPELINE_ENV:
        monkeypatch.delenv(key, raising=False)


def test_unset_env_keeps_ingestion_and_worker_on_the_same_local_pipeline(
    cleared_pipeline_env,
):
    from app.config import Settings as IngestionSettings
    from worker_app.config import Settings as WorkerSettings

    ingestion = IngestionSettings(_env_file=None)
    worker = WorkerSettings(_env_file=None)

    assert ingestion.use_localstack is True
    assert worker.use_localstack is True
    assert ingestion.aws_endpoint == worker.aws_endpoint == "http://localhost:4566"
    assert ingestion.aws_access_key_id == worker.aws_access_key_id == "test"
    assert ingestion.aws_secret_access_key == worker.aws_secret_access_key == "test"
    assert ingestion.aws_region == worker.aws_region == "us-east-1"
    assert ingestion.bucket_name == worker.bucket_name == "doc-storage"
    assert (
        ingestion.queue_url
        == worker.queue_url
        == "http://localhost:4566/000000000000/jobs"
    )
    assert ingestion.dynamodb_table == worker.dynamodb_table == "ProcessLog"
    assert (
        ingestion.ollama_base_url
        == worker.ollama_base_url
        == "http://localhost:11434"
    )
    assert ingestion.ollama_model == worker.ollama_model == "llama3:latest"
    assert (
        ingestion.ollama_http_timeout_seconds
        == worker.ollama_http_timeout_seconds
        == 600.0
    )
    assert ingestion.otlp_endpoint == worker.otlp_endpoint == "localhost:4317"

    assert worker.ingestion_base_url == "http://127.0.0.1:8000"
    assert worker.llm_mock_json is None
    assert ingestion.chroma_persist_dir == ".chroma_data"
    assert ingestion.embedding_model_name == "all-MiniLM-L6-v2"
