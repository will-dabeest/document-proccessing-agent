"""Unit tests for shared.llm_telemetry helpers."""

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


def test_apply_ollama_response_to_span_sets_standard_attributes():
    span = MagicMock()

    apply_ollama_response_to_span(
        span,
        response_json={
            "prompt_eval_count": 10,
            "eval_count": 3,
            "total_duration": 1_000_000_000,
            "done": True,
        },
        latency_ms=12.3456,
        prompt_char_len=42,
        model="llama3:latest",
        http_status_code=200,
        outcome="ok",
        stage="ingestion.rag",
        context_char_len=128,
        rag_n_requested=4,
        rag_n_returned=2,
        rag_empty_retrieval=False,
        rag_distance_min=0.1,
        rag_distance_max=0.5,
        rag_distance_mean=0.3,
        attempts=1,
    )

    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["gen_ai.system"] == "ollama"
    assert attrs["gen_ai.request.model"] == "llama3:latest"
    assert attrs["llm.latency_ms"] == 12.346
    assert attrs["llm.prompt_chars"] == 42
    assert attrs["llm.outcome"] == "ok"
    assert attrs["llm.stage"] == "ingestion.rag"
    assert attrs["http.status_code"] == 200
    assert attrs["llm.context_chars"] == 128
    assert attrs["rag.n_results_requested"] == 4
    assert attrs["rag.n_chunks_returned"] == 2
    assert attrs["rag.empty_retrieval"] is False
    assert attrs["rag.distance_min"] == 0.1
    assert attrs["rag.distance_max"] == 0.5
    assert attrs["rag.distance_mean"] == 0.3
    assert attrs["llm.attempts"] == 1
    assert attrs["llm.usage.prompt_eval_count"] == 10
    assert attrs["llm.usage.eval_count"] == 3
    assert attrs["llm.usage.total_duration"] == 1_000_000_000


def test_log_llm_event_emits_sorted_json_payload(caplog):
    logger = logging.getLogger("test.llm.telemetry")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_llm_event(
            logger,
            outcome="empty_retrieval",
            stage="rag.answer_question",
            rag_empty_retrieval=True,
            ignored_none=None,
        )

    assert (
        'llm_event {"outcome": "empty_retrieval", '
        '"rag_empty_retrieval": true, "stage": "rag.answer_question"}'
    ) in caplog.text
    assert "ignored_none" not in caplog.text
