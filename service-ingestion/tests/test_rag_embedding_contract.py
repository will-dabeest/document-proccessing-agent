"""Chroma must receive plain float lists, and retrieval spans stay named."""

from unittest.mock import MagicMock, patch

import numpy as np

from app.rag_service import index_document, retrieve_context, settings


def test_index_document_passes_python_float_lists_to_chroma():
    """ndarray embeddings break Chroma add(); .tolist() is the storage contract."""
    text = "hello world"
    model = MagicMock()
    model.encode.return_value = np.array([[1.0, 2.5, -3.0, 4.0]], dtype=np.float32)
    collection = MagicMock()

    with patch("app.rag_service._get_model", return_value=model), patch(
        "app.rag_service._get_collection", return_value=collection
    ):
        index_document("job-emb", "notes.txt", text)

    collection.add.assert_called_once()
    embeddings = collection.add.call_args.kwargs["embeddings"]
    assert type(embeddings) is list
    assert type(embeddings[0]) is list
    assert [type(value) for value in embeddings[0]] == [float, float, float, float]
    assert embeddings == [[1.0, 2.5, -3.0, 4.0]]


def test_retrieve_context_opens_embed_and_chroma_spans():
    question = "where is the policy?"
    model = MagicMock()
    model.encode.return_value = np.array([[0.25, 0.5, 0.75, 1.0]], dtype=np.float32)
    collection = MagicMock()
    collection.query.return_value = {
        "documents": [["only chunk"]],
        "metadatas": [[{"file": "a.txt"}]],
    }
    opened: list[tuple[str, MagicMock]] = []

    def start(name):
        span = MagicMock()
        opened.append((name, span))
        context = MagicMock()
        context.__enter__.return_value = span
        context.__exit__.return_value = False
        return context

    with patch("app.rag_service._get_model", return_value=model), patch(
        "app.rag_service._get_collection", return_value=collection
    ), patch("app.rag_service.tracer") as tracer:
        tracer.start_as_current_span.side_effect = start
        docs, _metas, distances = retrieve_context(question, n_results=3)

    assert docs == ["only chunk"]
    assert distances == []
    assert [name for name, _span in opened] == [
        "rag.retrieve_context",
        "rag.embed_query",
        "rag.chroma.query",
    ]
    outer_attrs = {
        call.args[0]: call.args[1] for call in opened[0][1].set_attribute.call_args_list
    }
    assert outer_attrs["rag.embedding_model"] == settings.embedding_model_name
    assert outer_attrs["rag.question_chars"] == len(question)
    assert outer_attrs["rag.n_results_requested"] == 3
    for _name, span in opened[1:]:
        span.set_attribute.assert_not_called()
