from unittest.mock import patch

from app.rag_service import answer_question


def test_answer_question_no_context_returns_fixed_message():
    with patch("app.rag_service.retrieve_context", return_value=([], [], [])):
        out = answer_question("What is X?", llm_call=lambda p: "should not run")
    assert out["answer"] == "No relevant information found in uploaded documents."
    assert out["snippets"] == []


def test_answer_question_empty_retrieval_requests_three_chunks_and_skips_llm(caplog):
    """Empty corpora must not call the LLM, and must still ask Chroma for three chunks."""
    import logging

    question = "where is the retention policy?"

    def llm_call(_prompt: str) -> str:
        raise AssertionError("llm should not run when retrieval is empty")

    caplog.set_level(logging.INFO, logger="app.rag_service")
    with patch("app.rag_service.retrieve_context", return_value=([], [], [])) as retrieve:
        out = answer_question(question, llm_call=llm_call)

    retrieve.assert_called_once_with(question, n_results=3)
    assert out == {
        "answer": "No relevant information found in uploaded documents.",
        "snippets": [],
    }
    assert (
        "llm_event "
        '{"outcome": "empty_retrieval", "rag_empty_retrieval": true, '
        f'"rag_question_chars": {len(question)}, "span": "rag.answer_question"}}'
    ) in caplog.text
    assert question not in caplog.text


def test_answer_question_calls_llm_and_returns_snippets():
    docs = ["ctx line one", "ctx two"]
    metas = [{"file": "a.txt"}, {"file": "b.txt"}]
    captured = {}

    def llm_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return "synthesized answer"

    with patch("app.rag_service.retrieve_context", return_value=(docs, metas, [])):
        out = answer_question("Q?", llm_call=llm_call)

    assert out["answer"] == "synthesized answer"
    assert "ctx line one" in captured["prompt"]
    assert "Q?" in captured["prompt"]
    assert len(out["snippets"]) == 2
    assert out["snippets"][0]["text"] == "ctx line one"
    assert out["snippets"][0]["metadata"] == {"file": "a.txt"}
