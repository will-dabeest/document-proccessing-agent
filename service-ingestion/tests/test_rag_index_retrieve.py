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


def test_index_document_reindex_reuses_deterministic_ids(fake_model, fake_collection):
    """Sync upload index then worker notify_index both call add with the same job_id ids."""
    text = "hello world"
    chunks = chunk_text(text)
    job_id = "jid-reindex"
    expected_ids = [f"{job_id}-{i}" for i in range(len(chunks))]

    with patch("app.rag_service._get_model", return_value=fake_model), patch(
        "app.rag_service._get_collection", return_value=fake_collection
    ):
        index_document(job_id, "a.txt", text)
        index_document(job_id, "a.txt", text)

    assert fake_collection.add.call_count == 2
    first_ids = fake_collection.add.call_args_list[0].kwargs["ids"]
    second_ids = fake_collection.add.call_args_list[1].kwargs["ids"]
    assert first_ids == expected_ids
    assert second_ids == expected_ids


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
