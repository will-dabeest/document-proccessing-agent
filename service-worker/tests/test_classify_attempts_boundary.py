"""Repair still runs when classify attempts is exactly one below the cutoff."""

from unittest.mock import patch

import worker_app.langgraph_flow as lg


def test_classify_node_attempts_one_still_repairs_then_accepts_json():
    calls: list[tuple[str, dict]] = []

    def fake_generate(prompt: str, **kwargs: object) -> str:
        calls.append((prompt, kwargs))
        if len(calls) == 1:
            return "DOC_MARKER_attempts_one_raw"
        return (
            '{"classification": "Coverage", '
            '"summary": "DOC_MARKER_attempts_one_fixed"}'
        )

    state = {
        "document_text": "DOC_MARKER_attempts_one_doc",
        "attempts": 1,
        "job_id": "job-attempts-one",
        "s3_key": "reports/attempts-one.txt",
    }
    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node(state)

    assert len(calls) == 2
    assert calls[0][1]["stage"] == "worker.classify"
    assert calls[0][1]["attempts"] == 1
    assert calls[0][1]["job_id"] == "job-attempts-one"
    assert calls[1][1]["stage"] == "worker.classify_json_repair"
    assert calls[1][1]["attempts"] == 1
    assert calls[1][1]["job_id"] == "job-attempts-one"
    assert "DOC_MARKER_attempts_one_raw" in calls[1][0]
    assert out["classification"] == "Coverage"
    assert out["summary"] == "DOC_MARKER_attempts_one_fixed"
    assert out["attempts"] == 2
    assert out["job_id"] == "job-attempts-one"
    assert out["s3_key"] == "reports/attempts-one.txt"
    assert out["document_text"] == "DOC_MARKER_attempts_one_doc"


def test_classify_node_attempts_one_failed_repair_keeps_repair_text():
    calls: list[dict] = []

    def fake_generate(_prompt: str, **kwargs: object) -> str:
        calls.append(kwargs)
        if len(calls) == 1:
            return "DOC_MARKER_attempts_one_first"
        return "DOC_MARKER_attempts_one_repair_fail"

    state = {
        "document_text": "DOC_MARKER_attempts_one_fail_doc",
        "attempts": 1,
        "job_id": "job-attempts-one-fail",
        "s3_key": "reports/attempts-one-fail.txt",
    }
    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node(state)

    assert len(calls) == 2
    assert calls[0]["stage"] == "worker.classify"
    assert calls[0]["attempts"] == 1
    assert calls[1]["stage"] == "worker.classify_json_repair"
    assert calls[1]["attempts"] == 1
    assert calls[1]["job_id"] == "job-attempts-one-fail"
    assert out["classification"] == "Unknown"
    assert out["summary"] == "DOC_MARKER_attempts_one_repair_fail"
    assert "DOC_MARKER_attempts_one_first" not in out["summary"]
    assert out["attempts"] == 3
    assert out["job_id"] == "job-attempts-one-fail"
    assert out["s3_key"] == "reports/attempts-one-fail.txt"
