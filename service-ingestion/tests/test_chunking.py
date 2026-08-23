import pytest

from app.chunking import chunk_text


def test_chunk_text_overlap_invalid_raises():
    with pytest.raises(ValueError, match="chunk_size must be greater than overlap"):
        chunk_text("abc", chunk_size=100, overlap=100)


def test_chunk_text_empty_returns_empty_list():
    assert chunk_text("") == []


def test_chunk_text_shorter_than_chunk_single_chunk():
    assert chunk_text("hello", chunk_size=800, overlap=100) == ["hello"]


def test_chunk_text_multiple_chunks_with_overlap():
    # chunk_size=10, overlap=2 -> stride 8
    text = "0123456789" + "abcdefgh" + "ijklmnop"  # 26 chars
    chunks = chunk_text(text, chunk_size=10, overlap=2)
    assert chunks[0] == "0123456789"
    assert chunks[1] == "89abcdefgh"
    assert chunks[2] == "ghijklmnop"
    assert "".join(chunks[:1]) == text[:10]
    assert len(chunks) == 3


def test_chunk_text_exact_chunk_boundary():
    text = "0123456789"  # 10 chars
    assert chunk_text(text, chunk_size=10, overlap=2) == ["0123456789"]


def test_chunk_text_default_size_and_overlap_used_by_rag():
    text = "x" * 850
    chunks = chunk_text(text)
    assert len(chunks[0]) == 800
    assert chunks[1] == text[700:850]
