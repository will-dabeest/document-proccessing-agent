"""Unit tests for shared.llm_telemetry helpers."""

from shared.llm_telemetry import (
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


def test_ollama_usage_span_attributes_keeps_exact_zero_counts_and_durations():
    """Ollama reports 0 when a model is warm or generation produced no tokens.

    A falsy check would drop those zeros and hide an empty generation.
    """
    data = {
        "prompt_eval_count": 0,
        "eval_count": 0,
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_duration": 0,
        "eval_duration": 0.0,
    }
    attrs = ollama_usage_span_attributes(data)
    assert attrs == {
        "llm.usage.prompt_eval_count": 0,
        "llm.usage.eval_count": 0,
        "llm.usage.total_duration": 0,
        "llm.usage.load_duration": 0,
        "llm.usage.prompt_eval_duration": 0,
        "llm.usage.eval_duration": 0.0,
    }


def test_ollama_usage_span_attributes_skips_bools_and_numeric_strings():
    """bool is an int subclass; True must not be stored as usage 1."""
    data = {
        "eval_count": True,
        "prompt_eval_count": False,
        "total_duration": "0",
        "load_duration": None,
        "prompt_eval_duration": "12",
        "eval_duration": 0,
    }
    attrs = ollama_usage_span_attributes(data)
    assert attrs == {"llm.usage.eval_duration": 0}
