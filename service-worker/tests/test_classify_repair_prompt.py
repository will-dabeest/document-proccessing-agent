"""JSON repair must not resend the source document.

The first classify prompt already includes up to 8000 characters of the
document. The repair prompt only restates the invalid model output. Folding
the document into that second call would resend customer text and change the
repair task.
"""

from unittest.mock import patch

import worker_app.langgraph_flow as lg

_DOCUMENT = "DOC_MARKER_91af_customer_text"


def test_classify_repair_prompt_omits_document_body():
    invalid = "not-json-output"
    assert _DOCUMENT not in invalid
    prompts: list[str] = []

    def fake_generate(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return invalid
        return '{"classification": "Ops", "summary": "Repaired."}'

    with patch.object(lg, "_ollama_generate", side_effect=fake_generate):
        out = lg.classify_node(
            {
                "document_text": _DOCUMENT,
                "attempts": 0,
                "job_id": "job-repair-boundary",
            }
        )

    assert out["classification"] == "Ops"
    assert out["summary"] == "Repaired."
    assert out["attempts"] == 1
    assert len(prompts) == 2

    first, repair = prompts
    assert _DOCUMENT in first
    assert "Classify and summarize" in first

    assert "invalid JSON" in repair
    assert invalid in repair
    assert "classification" in repair
    assert "summary" in repair
    assert _DOCUMENT not in repair
    assert "Classify and summarize" not in repair
    assert "Document:" not in repair
