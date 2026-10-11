"""RAG answers must ground on chunk text without sending retrieval metadata to the LLM."""

import logging
from unittest.mock import patch

from app.rag_service import answer_question

_CHUNKS = ("ALPHA_CHUNK_TEXT", "BETA_CHUNK_TEXT")
_FILE_A = "SECRET_FILE_NAME.txt"
_FILE_B = "OTHER_SECRET_FILE.txt"
_JOB_A = "SECRET_JOB_ID"
_JOB_B = "OTHER_SECRET_JOB"
_QUESTION = "UNIQUE_QUESTION_TOKEN"


def test_answer_prompt_uses_chunks_and_omits_retrieval_metadata(caplog):
    docs = list(_CHUNKS)
    metas = [
        {"file": _FILE_A, "job_id": _JOB_A},
        {"file": _FILE_B, "job_id": _JOB_B},
    ]
    captured: dict[str, str] = {}

    def llm_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return "grounded answer"

    caplog.set_level(logging.INFO, logger="app.rag_service")
    with patch(
        "app.rag_service.retrieve_context",
        return_value=(docs, metas, [0.2, 0.4]),
    ):
        out = answer_question(_QUESTION, llm_call=llm_call)

    prompt = captured["prompt"]
    assert "ALPHA_CHUNK_TEXT\n---\nBETA_CHUNK_TEXT" in prompt
    assert prompt.index("ALPHA_CHUNK_TEXT") < prompt.index("BETA_CHUNK_TEXT")
    assert prompt.index("BETA_CHUNK_TEXT") < prompt.index(_QUESTION)
    assert "using only the context" in prompt
    assert "no relevant information found" in prompt
    for secret in (_FILE_A, _FILE_B, _JOB_A, _JOB_B):
        assert secret not in prompt

    assert out["answer"] == "grounded answer"
    assert out["snippets"] == [
        {"text": "ALPHA_CHUNK_TEXT", "metadata": {"file": _FILE_A, "job_id": _JOB_A}},
        {"text": "BETA_CHUNK_TEXT", "metadata": {"file": _FILE_B, "job_id": _JOB_B}},
    ]

    logged = caplog.text
    assert "llm_event" in logged
    assert "llm_prompt_chars" in logged
    assert "llm_context_chars" in logged
    assert "rag_question_chars" in logged
    for secret in (*_CHUNKS, _QUESTION, _FILE_A, _FILE_B, _JOB_A, _JOB_B):
        assert secret not in logged
