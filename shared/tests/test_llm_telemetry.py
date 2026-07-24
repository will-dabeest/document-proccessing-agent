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


def test_flatten_distances_skips_non_numeric_entries():
    assert flatten_distances([[0.25, "bad", None, 0.75]]) == [0.25, 0.75]
    assert flatten_distances(None) == []
    assert flatten_distances("not-a-list") == []


def test_ollama_usage_span_attributes_skips_bool_and_missing():
    attrs = ollama_usage_span_attributes(
        {
            "prompt_eval_count": True,
            "eval_count": "3",
            "total_duration": 9,
        }
    )
    assert attrs == {"llm.usage.total_duration": 9}
    assert ollama_usage_span_attributes(None) == {}


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
