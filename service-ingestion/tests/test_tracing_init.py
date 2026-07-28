"""Tracing bootstrap soft-fail coverage for the ingestion service."""

from unittest.mock import patch

from app.main import _configure_tracing


def test_configure_tracing_soft_fails_when_exporter_init_raises(caplog):
    with patch(
        "app.main.OTLPSpanExporter",
        side_effect=RuntimeError("otlp unavailable"),
    ):
        _configure_tracing()

    assert any("tracing_init_failed" in r.message for r in caplog.records)


def test_configure_tracing_soft_fails_when_provider_setup_raises(caplog):
    with patch(
        "app.main.TracerProvider",
        side_effect=RuntimeError("provider boom"),
    ):
        _configure_tracing()

    assert any("tracing_init_failed" in r.message for r in caplog.records)
