from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    use_localstack: bool = True
    aws_endpoint: str = Field(
        default="http://localhost:4566",
        validation_alias=AliasChoices("AWS_ENDPOINT_URL", "AWS_ENDPOINT"),
    )
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_region: str = "us-east-1"
    bucket_name: str = Field(
        default="doc-storage",
        validation_alias=AliasChoices("BUCKET_NAME", "bucket_name"),
    )
    queue_url: str = Field(
        default="http://localhost:4566/000000000000/jobs",
        validation_alias="QUEUE_URL",
    )
    dynamodb_table: str = Field(default="ProcessLog", validation_alias="DYNAMODB_TABLE")
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        validation_alias="OLLAMA_BASE_URL",
    )
    ollama_model: str = Field(default="llama3", validation_alias="OLLAMA_MODEL")
    ingestion_base_url: str = Field(
        default="http://127.0.0.1:8000",
        validation_alias="INGESTION_BASE_URL",
    )
    otlp_endpoint: str = Field(
        default="localhost:4317",
        validation_alias=AliasChoices("OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    llm_mock_json: str | None = Field(default=None, validation_alias="LLM_MOCK_JSON")


settings = Settings()
