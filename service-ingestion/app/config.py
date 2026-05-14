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
    ollama_model: str = Field(
        default="llama3:latest",
        validation_alias="OLLAMA_MODEL",
    )
    ollama_http_timeout_seconds: float = Field(
        default=600.0,
        validation_alias="OLLAMA_HTTP_TIMEOUT_SECONDS",
    )
    otlp_endpoint: str = Field(
        default="localhost:4317",
        validation_alias=AliasChoices("OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    chroma_persist_dir: str = Field(
        default=".chroma_data",
        validation_alias="CHROMA_PERSIST_DIR",
    )
    embedding_model_name: str = Field(
        default="all-MiniLM-L6-v2",
        validation_alias="EMBEDDING_MODEL_NAME",
    )
    url_import_enabled: bool = Field(
        default=True,
        validation_alias="URL_IMPORT_ENABLED",
    )
    url_import_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        validation_alias="URL_IMPORT_MAX_BYTES",
    )
    url_import_max_redirects: int = Field(
        default=5,
        validation_alias="URL_IMPORT_MAX_REDIRECTS",
    )
    url_import_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="URL_IMPORT_TIMEOUT_SECONDS",
    )


settings = Settings()
