"""Unit tests for OTLP endpoint normalization used by tracing init."""

from app.main import _normalize_otlp_endpoint


def test_normalize_otlp_endpoint_strips_http_and_https_prefixes():
    assert _normalize_otlp_endpoint("https://jaeger:4317") == "jaeger:4317"
    assert _normalize_otlp_endpoint("http://localhost:4317") == "localhost:4317"


def test_normalize_otlp_endpoint_leaves_bare_host_port():
    assert _normalize_otlp_endpoint("localhost:4317") == "localhost:4317"
    assert _normalize_otlp_endpoint("collector.internal:4317") == "collector.internal:4317"


def test_normalize_otlp_endpoint_trims_whitespace():
    assert _normalize_otlp_endpoint("  https://collector:4317  ") == "collector:4317"
    assert _normalize_otlp_endpoint("\thttp://otel:4317\n") == "otel:4317"
