import pytest

from app.service.chunker import TextChunker
from tests.unit.fakes import FakeEncoder


def make_chunker(chunk_size: int, overlap: int) -> TextChunker:
    return TextChunker(chunk_size=chunk_size, overlap=overlap, encoder=FakeEncoder())


class TestTextChunker:
    def test_empty_text_returns_no_chunks(self):
        chunker = make_chunker(100, 10)
        assert chunker.chunk("") == []
        assert chunker.chunk("   \n\n  ") == []

    def test_short_text_produces_single_chunk(self):
        chunker = make_chunker(100, 10)
        chunks = chunker.chunk("This is a short paragraph.")
        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert chunks[0].token_count > 0

    def test_paragraph_boundary_splits(self):
        chunker = make_chunker(4, 0)
        text = "para one here.\n\npara two here.\n\npara three here."
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_long_paragraph_is_sliced(self):
        chunker = make_chunker(20, 5)
        long_para = ("word " * 200).strip()
        chunks = chunker.chunk(long_para)
        assert len(chunks) > 1
        for c in chunks:
            assert c.token_count <= 20 + 5

    def test_overlap_creates_distinct_chunks(self):
        chunker = make_chunker(10, 3)
        text = " ".join(f"w{i}" for i in range(40))
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2
        contents = {c.content for c in chunks}
        assert len(contents) > 1

    def test_invalid_overlap_config_raises(self):
        with pytest.raises(ValueError):
            TextChunker(chunk_size=10, overlap=10, encoder=FakeEncoder())
        with pytest.raises(ValueError):
            TextChunker(chunk_size=10, overlap=20, encoder=FakeEncoder())
