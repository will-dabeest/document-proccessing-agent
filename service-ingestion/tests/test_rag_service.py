from unittest.mock import patch

from app.rag_service import answer_question, index_document


def test_index_document_skips_blank_or_whitespace_text():
    with patch("app.rag_service.chunk_text") as chunk, patch(
        "app.rag_service._get_model"
    ) as model, patch("app.rag_service._get_collection") as coll:
        index_document("job-1", "blank.txt", "   \n\t  ")

    chunk.assert_not_called()
    model.assert_not_called()
    coll.assert_not_called()


def test_internal_index_route_indexes_and_returns_status():
    from fastapi.testclient import TestClient

    from app.main import app

    with patch("app.main.index_document") as idx:
        client = TestClient(app)
        response = client.post(
            "/internal/index",
            json={"job_id": "job-9", "filename": "a.txt", "text": "indexed body"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "indexed"}
    idx.assert_called_once_with("job-9", "a.txt", "indexed body")


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
