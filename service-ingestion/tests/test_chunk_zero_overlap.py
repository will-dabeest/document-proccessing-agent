"""Zero overlap is legal and must not drop or repeat characters.

Overlap equal to the chunk size is rejected because the stride would be
zero. Overlap zero is the other side of that guard: the stride equals the
chunk size, adjacent pieces abut, and a short tail is kept.
"""

from app.chunking import chunk_text


def test_chunk_text_zero_overlap_abuts_without_gaps_or_repeats():
    exact = chunk_text("abcdefgh", chunk_size=4, overlap=0)
    remainder = chunk_text("abcdefghij", chunk_size=4, overlap=0)

    assert exact == ["abcd", "efgh"]
    assert remainder == ["abcd", "efgh", "ij"]
    assert "".join(remainder) == "abcdefghij"
