from dataclasses import dataclass

import tiktoken


@dataclass(frozen=True)
class TextChunk:
    index: int
    content: str
    token_count: int


class TextChunker:
    """토큰 기반 의미 단위 청킹.

    1) 문단(\\n\\n) 기준으로 우선 분할
    2) 청크가 max를 초과하면 문장 단위로 재분할
    3) overlap 적용
    """

    def __init__(self, *, chunk_size: int, overlap: int, encoder=None) -> None:
        if chunk_size <= overlap:
            raise ValueError("chunk_size must be greater than overlap")
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._encoder = encoder or tiktoken.get_encoding("cl100k_base")

    def chunk(self, text: str) -> list[TextChunk]:
        text = text.strip()
        if not text:
            return []

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: list[str] = []
        buffer: list[str] = []
        buffer_tokens = 0

        for para in paragraphs:
            para_tokens = self._count(para)

            if para_tokens > self._chunk_size:
                # 너무 긴 문단은 토큰 단위 슬라이싱
                if buffer:
                    chunks.append("\n\n".join(buffer))
                    buffer, buffer_tokens = [], 0
                chunks.extend(self._slice_long_text(para))
                continue

            if buffer_tokens + para_tokens > self._chunk_size:
                chunks.append("\n\n".join(buffer))
                buffer, buffer_tokens = [], 0

            buffer.append(para)
            buffer_tokens += para_tokens

        if buffer:
            chunks.append("\n\n".join(buffer))

        chunks = self._apply_overlap(chunks)

        return [
            TextChunk(index=i, content=c, token_count=self._count(c))
            for i, c in enumerate(chunks)
        ]

    def _count(self, text: str) -> int:
        return len(self._encoder.encode(text))

    def _slice_long_text(self, text: str) -> list[str]:
        tokens = self._encoder.encode(text)
        result = []
        step = self._chunk_size - self._overlap
        for start in range(0, len(tokens), step):
            window = tokens[start : start + self._chunk_size]
            result.append(self._encoder.decode(window))
            if start + self._chunk_size >= len(tokens):
                break
        return result

    def _apply_overlap(self, chunks: list[str]) -> list[str]:
        if self._overlap == 0 or len(chunks) <= 1:
            return chunks

        result = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_tokens = self._encoder.encode(chunks[i - 1])
            tail = self._encoder.decode(prev_tokens[-self._overlap :])
            result.append(f"{tail}\n\n{chunks[i]}")
        return result
