import threading

import pytest

from app.chunking import chunk_text


def _chunk_text_bounded(text: str, *, chunk_size: int, overlap: int, timeout: float = 1.0):
    """Call chunk_text without letting a negative stride hang the suite.

    overlap > chunk_size makes ``start`` move backward. If the guard regresses
    from ``<=`` to ``==``, or is removed, ``chunk_text("abcd", 2, 3)`` never
    returns. A daemon thread turns that into a failed assertion.
    """
    outcome: dict = {}

    def _run() -> None:
        try:
            outcome["chunks"] = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(
            "chunk_text did not finish; chunk_size must stay greater than overlap"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["chunks"]


def test_chunk_text_overlap_invalid_raises():
    with pytest.raises(ValueError, match="chunk_size must be greater than overlap"):
        chunk_text("abc", chunk_size=100, overlap=100)


def test_chunk_text_overlap_greater_than_size_raises():
    # Stride would be 2 - 3 = -1, so the guard must reject this before the loop.
    with pytest.raises(ValueError, match="chunk_size must be greater than overlap"):
        _chunk_text_bounded("abcd", chunk_size=2, overlap=3)


def test_chunk_text_minimum_positive_stride_stops():
    # chunk_size == overlap + 1 is the smallest stride that must still be legal.
    assert _chunk_text_bounded("abcd", chunk_size=2, overlap=1) == ["ab", "bc", "cd"]


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
