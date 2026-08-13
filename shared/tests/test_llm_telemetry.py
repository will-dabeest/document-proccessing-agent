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


def test_apply_ollama_response_to_span_sets_core_and_optional_attrs():
    span = MagicMock()
    apply_ollama_response_to_span(
        span,
        response_json={"prompt_eval_count": 4, "eval_count": 2},
        latency_ms=12.3456,
        prompt_char_len=10,
        model="llama3:latest",
        http_status_code=200,
        outcome="ok",
        stage="ingestion.rag",
        rag_n_requested=3,
        rag_n_returned=1,
        rag_empty_retrieval=False,
        attempts=0,
    )
    attrs = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
    assert attrs["gen_ai.system"] == "ollama"
    assert attrs["gen_ai.request.model"] == "llama3:latest"
    assert attrs["llm.latency_ms"] == 12.346
    assert attrs["llm.prompt_chars"] == 10
    assert attrs["llm.outcome"] == "ok"
    assert attrs["llm.stage"] == "ingestion.rag"
    assert attrs["http.status_code"] == 200
    assert attrs["rag.n_results_requested"] == 3
    assert attrs["rag.n_chunks_returned"] == 1
    assert attrs["rag.empty_retrieval"] is False
    assert attrs["llm.attempts"] == 0
    assert attrs["llm.usage.prompt_eval_count"] == 4
    assert attrs["llm.usage.eval_count"] == 2


def test_apply_ollama_response_to_span_skips_none_model_and_optional_fields():
    span = MagicMock()
    apply_ollama_response_to_span(
        span,
        response_json=None,
        latency_ms=1.0,
        prompt_char_len=3,
        model=None,
        http_status_code=None,
        outcome="exception",
    )
    keys = [c.args[0] for c in span.set_attribute.call_args_list]
    assert "gen_ai.request.model" not in keys
    assert "http.status_code" not in keys
    assert "llm.stage" not in keys
    assert "rag.empty_retrieval" not in keys
    attrs = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
    assert attrs["gen_ai.system"] == "ollama"
    assert attrs["llm.outcome"] == "exception"
