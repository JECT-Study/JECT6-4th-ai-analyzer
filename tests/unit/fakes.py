"""tiktoken 다운로드를 피하기 위한 가짜 인코더.

토큰을 단순히 공백으로 분리한 단어 단위로 카운트한다.
"""
from __future__ import annotations


class FakeEncoder:
    def encode(self, text: str) -> list[int]:
        return [hash(token) & 0xFFFF for token in text.split()]

    def decode(self, tokens: list[int]) -> str:
        # 디코딩이 정확할 필요는 없고 길이/존재만 의미 있음
        return " ".join("tok" for _ in tokens)
