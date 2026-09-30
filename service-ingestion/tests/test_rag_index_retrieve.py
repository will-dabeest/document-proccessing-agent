"""Tests for index_document and retrieve_context with mocked model and Chroma."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.chunking import chunk_text
from app.rag_service import index_document, retrieve_context


@pytest.fixture
def fake_collection():
    col = MagicMock()
    col.query.return_value = {
        "documents": [["chunk-a", "chunk-b"]],
        "metadatas": [[{"file": "f.txt"}, {"file": "g.txt"}]],
        "distances": [[0.1, 0.2]],
    }
    return col


@pytest.fixture
def fake_model():
    m = MagicMock()

    def encode(texts):
        return np.zeros((len(texts), 4), dtype=np.float32)

    m.encode.side_effect = encode
    return m


def test_index_document_whitespace_only_skips_model_and_collection():
    with patch("app.rag_service._get_model") as gm, patch(
        "app.rag_service._get_collection"
    ) as gc:
        index_document("job-1", "x.txt", "  \n\t  ")
    gm.assert_not_called()
    gc.assert_not_called()


def test_index_document_empty_string_skips():
    with patch("app.rag_service._get_model") as gm, patch(
        "app.rag_service._get_collection"
    ) as gc:
        index_document("job-1", "x.txt", "")
    gm.assert_not_called()
    gc.assert_not_called()


def test_index_document_adds_chunks_with_ids_and_metadata(fake_model, fake_collection):
    text = "hello world"
    chunks = chunk_text(text)
    job_id = "jid-42"
    filename = "notes.txt"

    with patch("app.rag_service._get_model", return_value=fake_model), patch(
        "app.rag_service._get_collection", return_value=fake_collection
    ):
        index_document(job_id, filename, text)

    fake_model.encode.assert_called_once_with(chunks)
    fake_collection.add.assert_called_once()
    call_kw = fake_collection.add.call_args.kwargs
    assert call_kw["documents"] == chunks
    assert call_kw["ids"] == [f"{job_id}-{i}" for i in range(len(chunks))]
    assert call_kw["metadatas"] == [
        {"file": filename, "job_id": job_id} for _ in chunks
    ]
    emb = call_kw["embeddings"]
    assert len(emb) == len(chunks)
    assert all(len(row) == 4 for row in emb)


def test_retrieve_context_passes_n_results_to_query(fake_model, fake_collection):
    with patch("app.rag_service._get_model", return_value=fake_model), patch(
        "app.rag_service._get_collection", return_value=fake_collection
    ):
        docs, metas, dists = retrieve_context("What is the policy?", n_results=7)

    fake_collection.query.assert_called_once()
    qcall = fake_collection.query.call_args
    assert qcall.kwargs["n_results"] == 7
    assert qcall.kwargs["include"] == ["documents", "metadatas", "distances"]
    q_emb = qcall.kwargs["query_embeddings"]
    assert len(q_emb) == 1
    assert len(q_emb[0]) == 4
    assert docs == ["chunk-a", "chunk-b"]
    assert metas == [{"file": "f.txt"}, {"file": "g.txt"}]
    assert dists == [0.1, 0.2]


def test_retrieve_context_records_exact_zero_and_numeric_string_distances(
    fake_model, fake_collection
):
    """Chroma may report 0.0 for an exact hit, or numeric strings; both must survive."""
    fake_collection.query.return_value = {
        "documents": [["exact match", "nearby"]],
        "metadatas": [[{"file": "a.txt"}, {"file": "b.txt"}]],
        "distances": [["0.0", 2]],
    }
    span = MagicMock()
    span.__enter__.return_value = span
    span.__exit__.return_value = False
    with patch("app.rag_service._get_model", return_value=fake_model), patch(
        "app.rag_service._get_collection", return_value=fake_collection
    ), patch("app.rag_service.tracer") as tracer:
        tracer.start_as_current_span.return_value = span
        docs, _metas, dists = retrieve_context("exact?", n_results=2)

    assert docs == ["exact match", "nearby"]
    assert dists == [0.0, 2.0]
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["rag.distance_min"] == 0.0
    assert attrs["rag.distance_max"] == 2.0
    assert attrs["rag.distance_mean"] == 1.0
    assert attrs["rag.empty_retrieval"] is False
    assert attrs["rag.n_chunks_returned"] == 2


def test_retrieve_context_default_n_results_is_three(fake_model, fake_collection):
    with patch("app.rag_service._get_model", return_value=fake_model), patch(
        "app.rag_service._get_collection", return_value=fake_collection
    ):
        retrieve_context("Q?")

    assert fake_collection.query.call_args.kwargs["n_results"] == 3
    assert fake_collection.query.call_args.kwargs["include"] == [
        "documents",
        "metadatas",
        "distances",
    ]
