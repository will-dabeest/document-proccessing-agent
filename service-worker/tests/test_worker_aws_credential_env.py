"""Worker AWS clients send whatever Settings loaded; the env binding is separate."""

from worker_app.config import Settings


def test_aws_credential_env_overrides_localstack_defaults(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "override-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "override-secret-key")

    settings = Settings(_env_file=None)

    assert settings.aws_access_key_id == "override-access-key"
    assert settings.aws_secret_access_key == "override-secret-key"
