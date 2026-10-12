"""Worker tracing exports to the normalized OTLP endpoint without TLS."""

from unittest.mock import MagicMock, patch

from worker_app.worker import _configure_tracing


def test_configure_tracing_uses_worker_service_name_and_insecure_otlp(monkeypatch, caplog):
    from worker_app.worker import settings

    monkeypatch.setattr(settings, "otlp_endpoint", "  https://jaeger:4317  ")
    exporter = object()
    resource = object()
    provider = MagicMock()

    with patch(
        "worker_app.worker.Resource.create", return_value=resource
    ) as create_resource, patch(
        "worker_app.worker.TracerProvider", return_value=provider
    ) as provider_cls, patch(
        "worker_app.worker.OTLPSpanExporter", return_value=exporter
    ) as exporter_cls, patch(
        "worker_app.worker.BatchSpanProcessor", return_value="processor"
    ) as processor_cls, patch(
        "worker_app.worker.trace.set_tracer_provider"
    ) as set_provider:
        _configure_tracing()

    assert not any("tracing_init_failed" in record.message for record in caplog.records)
    create_resource.assert_called_once_with({"service.name": "service-worker"})
    provider_cls.assert_called_once_with(resource=resource)
    exporter_cls.assert_called_once_with(endpoint="jaeger:4317", insecure=True)
    processor_cls.assert_called_once_with(exporter)
    provider.add_span_processor.assert_called_once_with("processor")
    set_provider.assert_called_once_with(provider)
