"""Env names Compose and operators set that ingestion Settings must still read.

Worker tests already accept AWS_ENDPOINT_URL. Ingestion tests only accept the
alternate AWS_ENDPOINT name. URL import byte and enable flags are pinned
elsewhere; the redirect and timeout caps are not.
"""

from app.config import Settings


def test_aws_endpoint_url_wins_over_alternate_endpoint_name(monkeypatch):
    """Compose sets AWS_ENDPOINT_URL. That name must beat AWS_ENDPOINT."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.setenv("AWS_ENDPOINT", "http://should-not-win:9999")

    settings = Settings(_env_file=None)

    assert settings.aws_endpoint == "http://localstack:4566"


def test_standard_otlp_env_applies_when_compose_name_is_unset(monkeypatch):
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")

    settings = Settings(_env_file=None)

    assert settings.otlp_endpoint == "otel-collector:4317"


def test_url_import_redirect_and_timeout_caps_follow_env(monkeypatch):
    monkeypatch.setenv("URL_IMPORT_MAX_REDIRECTS", "1")
    monkeypatch.setenv("URL_IMPORT_TIMEOUT_SECONDS", "4.5")

    settings = Settings(_env_file=None)

    assert settings.url_import_max_redirects == 1
    assert settings.url_import_timeout_seconds == 4.5


def test_aws_credential_env_overrides_localstack_defaults(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "override-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "override-secret-key")

    settings = Settings(_env_file=None)

    assert settings.aws_access_key_id == "override-access-key"
    assert settings.aws_secret_access_key == "override-secret-key"
