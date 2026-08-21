"""Unit tests for shared.llm_telemetry helpers."""

import json
import logging

from shared.llm_telemetry import (
    flatten_distances,
    log_llm_event,
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


def test_log_llm_event_omits_none_but_keeps_falsey_values(caplog):
    """None is dropped; False/0/'' must remain so empty-retrieval and zero-latency stay visible."""
    logger = logging.getLogger("test.llm_telemetry")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_llm_event(
            logger,
            span="rag.retrieve_context",
            outcome="ok",
            rag_empty_retrieval=False,
            rag_n_chunks_returned=0,
            error=None,
            latency_ms=0,
            model="",
        )

    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert line.startswith("llm_event ")
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["rag_empty_retrieval"] is False
    assert payload["rag_n_chunks_returned"] == 0
    assert payload["latency_ms"] == 0
    assert payload["model"] == ""
    assert "error" not in payload
