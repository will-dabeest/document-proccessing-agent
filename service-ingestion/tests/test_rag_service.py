from unittest.mock import patch

from app.rag_service import answer_question


def test_answer_question_no_context_returns_fixed_message():
    with patch("app.rag_service.retrieve_context", return_value=([], [], [])):
        out = answer_question("What is X?", llm_call=lambda p: "should not run")
    assert out["answer"] == "No relevant information found in uploaded documents."
    assert out["snippets"] == []


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


def test_answer_question_uneven_docs_metas_truncates_snippets():
    """Prompt uses all docs; snippets follow zip(strict=False) and stop at shorter metas."""
    docs = ["chunk-a", "chunk-b"]
    metas = [{"file": "a.txt"}]
    captured = {}

    def llm_call(prompt: str) -> str:
        captured["prompt"] = prompt
        return "answer"

    with patch("app.rag_service.retrieve_context", return_value=(docs, metas, [])):
        out = answer_question("Q?", llm_call=llm_call)

    assert "chunk-a" in captured["prompt"]
    assert "chunk-b" in captured["prompt"]
    assert out["snippets"] == [{"text": "chunk-a", "metadata": {"file": "a.txt"}}]


def test_answer_question_empty_metas_yields_empty_snippets():
    docs = ["only-doc"]
    with patch("app.rag_service.retrieve_context", return_value=(docs, [], [])):
        out = answer_question("Q?", llm_call=lambda p: "ok")
    assert out["answer"] == "ok"
    assert out["snippets"] == []
