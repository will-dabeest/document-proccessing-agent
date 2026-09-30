"""Unit tests for shared.llm_telemetry helpers."""

import json
import logging
from unittest.mock import MagicMock

from shared.llm_telemetry import (
    apply_ollama_response_to_span,
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


def test_rag_distance_stats_keeps_exact_zero():
    """A distance of 0 is an exact match, not an empty sample."""
    assert rag_distance_stats([0.0, 0.0]) == (0.0, 0.0, 0.0)


def test_flatten_distances_nested():
    assert flatten_distances([[0.5, 1.0]]) == [0.5, 1.0]


def test_flatten_distances_flat():
    assert flatten_distances([0.1, 0.2]) == [0.1, 0.2]


def test_flatten_distances_coerces_numeric_strings_and_keeps_zero():
    assert flatten_distances([["0.0", 0, "", "nope"]]) == [0.0, 0.0]
    assert flatten_distances([0]) == [0.0]


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


def test_apply_ollama_response_to_span_records_zero_distances_and_counts():
    """Falsy zeros must stay on the span; `if value:` would drop exact-match distances."""
    span = MagicMock()
    apply_ollama_response_to_span(
        span,
        response_json=None,
        latency_ms=0.0,
        prompt_char_len=0,
        model="llama3",
        http_status_code=200,
        outcome="ok",
        context_char_len=0,
        rag_n_requested=0,
        rag_n_returned=0,
        rag_distance_min=0.0,
        rag_distance_max=0.0,
        rag_distance_mean=0.0,
    )
    attrs = {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}
    assert attrs["llm.latency_ms"] == 0.0
    assert attrs["llm.prompt_chars"] == 0
    assert attrs["llm.context_chars"] == 0
    assert attrs["rag.n_results_requested"] == 0
    assert attrs["rag.n_chunks_returned"] == 0
    assert attrs["rag.distance_min"] == 0.0
    assert attrs["rag.distance_max"] == 0.0
    assert attrs["rag.distance_mean"] == 0.0


class _Unserializable:
    def __str__(self) -> str:
        return "boom-detail"


def test_log_llm_event_stringifies_non_json_values(caplog):
    """Logging runs on the ask path; a non-JSON field must not raise."""
    logger = logging.getLogger("test.llm.telemetry.nonstr")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_llm_event(
            logger,
            span="llm.ollama.generate",
            outcome="exception",
            error=_Unserializable(),
        )

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("llm_event ")
    )
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["error"] == "boom-detail"
    assert payload["outcome"] == "exception"
