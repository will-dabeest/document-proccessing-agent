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


def test_log_llm_event_keeps_false_and_zero_and_drops_none(caplog):
    """`if value:` would hide empty retrieval and a zero latency from log search."""
    logger = logging.getLogger("test.llm.telemetry.falsy")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_llm_event(
            logger,
            span="rag.answer_question",
            outcome="ok",
            rag_empty_retrieval=False,
            latency_ms=0,
            prompt_chars=0,
            model="",
            rag_distance_min=None,
            error=None,
        )

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("llm_event ")
    )
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["rag_empty_retrieval"] is False
    assert payload["latency_ms"] == 0
    assert payload["prompt_chars"] == 0
    assert payload["model"] == ""
    assert "rag_distance_min" not in payload
    assert "error" not in payload
    assert list(payload) == sorted(payload)
