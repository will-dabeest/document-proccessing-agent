"""Unit tests for shared.llm_telemetry helpers."""

from unittest.mock import MagicMock

from shared.llm_telemetry import (
    apply_ollama_response_to_span,
    flatten_distances,
    ollama_usage_span_attributes,
    rag_distance_stats,
)


def test_rag_distance_stats_empty():
    assert rag_distance_stats([]) == (None, None, None)
    assert rag_distance_stats(None) == (None, None, None)


def test_rag_distance_stats_values():
    assert rag_distance_stats([0.1, 0.3, 0.2]) == (0.1, 0.3, 0.2)


def test_flatten_distances_nested():
    assert flatten_distances([[0.5, 1.0]]) == [0.5, 1.0]


def test_flatten_distances_flat():
    assert flatten_distances([0.1, 0.2]) == [0.1, 0.2]


def test_ollama_usage_span_attributes_extracts_known_keys():
    data = {
        "response": "hi",
        "prompt_eval_count": 10,
        "eval_count": 3,
        "total_duration": 1_000_000_000,
        "bogus": "x",
    }
    attrs = ollama_usage_span_attributes(data)
    assert attrs["llm.usage.prompt_eval_count"] == 10
    assert attrs["llm.usage.eval_count"] == 3
    assert attrs["llm.usage.total_duration"] == 1_000_000_000
    assert "bogus" not in attrs


def test_apply_ollama_response_to_span_does_not_record_raw_prompt():
    """Spans must carry prompt length only — never the raw prompt or completion text."""
    span = MagicMock()
    apply_ollama_response_to_span(
        span,
        response_json={"response": "SECRET_COMPLETION", "prompt_eval_count": 2},
        latency_ms=1.25,
        prompt_char_len=42,
        model="llama3:latest",
        http_status_code=200,
        outcome="ok",
        stage="ingestion.rag",
    )
    attrs = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
    assert attrs["llm.prompt_chars"] == 42
    assert attrs["gen_ai.system"] == "ollama"
    assert "SECRET_COMPLETION" not in attrs.values()
    assert "gen_ai.prompt" not in attrs
    assert "llm.prompt" not in attrs
    assert "llm.completion" not in attrs
    assert "gen_ai.completion" not in attrs
