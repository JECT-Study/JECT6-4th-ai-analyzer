from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import tiktoken
from openai import APIError, AsyncOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings
from app.core.exceptions import LLMClientError
from app.core.logging import get_logger
from app.domain.schemas import ChatMessage

try:
    from opentelemetry import trace as _otel_trace
    _tracer = _otel_trace.get_tracer(__name__)
    _OTEL_AVAILABLE = True
except ImportError:
    _otel_trace = None  # type: ignore[assignment]
    _tracer = None
    _OTEL_AVAILABLE = False

logger = get_logger(__name__)

_DEMO_ANALYSIS_RESULT = json.dumps({
    "summary": "블로그 분석 결과입니다. (데모 모드 고정값)",
    "key_topics": ["블로그", "리뷰", "체험단", "맛집", "뷰티"],
    "tone": "친근하고 감성적인 문체",
    "target_audience": "20-30대 여성 독자",
    "suggestions": ["사진 품질 향상", "SEO 키워드 추가"],
    "overall_score": 78,
    "percentile": 72,
    "blog_type": "라이프스타일 블로거",
    "strength_summary": "감성적인 사진과 솔직한 후기가 강점입니다.",
    "weakness_summary": "정보성 콘텐츠 보완 시 검색 유입이 증가합니다.",
    "top_categories": [
        {"category": "FOOD", "score": 85},
        {"category": "BEAUTY", "score": 72},
        {"category": "LIVING", "score": 60},
    ],
    "metrics": [
        {"name": "콘텐츠 품질", "score": 80},
        {"name": "정보 충실도", "score": 65},
        {"name": "사진 활용", "score": 88},
        {"name": "독자 친화도", "score": 76},
        {"name": "SEO 최적화", "score": 70},
        {"name": "일관성", "score": 82},
    ],
}, ensure_ascii=False)

_DEMO_EMBEDDING = [0.9 if i < 10 else 0.01 for i in range(768)]


class _NullSpan:
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def set_attribute(self, *_args, **_kwargs): pass
    def record_exception(self, *_args, **_kwargs): pass
    def set_status(self, *_args, **_kwargs): pass


def _span(name: str):
    if _OTEL_AVAILABLE and _tracer is not None:
        return _tracer.start_as_current_span(name)
    return _NullSpan()


class LLMClient:
    """LLM/임베딩 클라이언트.

    llm_provider 설정에 따라 OpenAI / Ollama / Demo 중 하나를 사용합니다.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        provider = self._settings.llm_provider.lower()

        if provider == "demo":
            self._client = None
        elif provider == "ollama":
            # Ollama는 OpenAI 호환 API를 제공하므로 base_url만 교체
            self._client = AsyncOpenAI(
                base_url=f"{self._settings.ollama_base_url}/v1",
                api_key="ollama",
            )
        elif provider == "gemini":
            # Google AI Studio OpenAI 호환 엔드포인트 사용
            self._client = AsyncOpenAI(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=self._settings.gemini_api_key,
            )
        else:
            # openai (default)
            self._client = AsyncOpenAI(api_key=self._settings.openai_api_key)

        self._semaphore = asyncio.Semaphore(self._settings.llm_max_concurrency)
        self._encoder = tiktoken.get_encoding("cl100k_base")

    def _chat_model(self) -> str:
        provider = self._settings.llm_provider.lower()
        if provider == "ollama":
            return self._settings.ollama_chat_model
        if provider == "gemini":
            return self._settings.gemini_chat_model
        return self._settings.llm_model

    def _embedding_model(self) -> str:
        provider = self._settings.llm_provider.lower()
        if provider == "ollama":
            return self._settings.ollama_embedding_model
        if provider == "gemini":
            return self._settings.gemini_embedding_model
        return self._settings.embedding_model

    def _supports_dimensions(self) -> bool:
        """OpenAI text-embedding-3-* / Gemini gemini-embedding-* 계열만 dimensions 파라미터를 지원한다."""
        provider = self._settings.llm_provider.lower()
        model = self._embedding_model()
        if provider == "openai":
            return model.startswith("text-embedding-3")
        if provider == "gemini":
            return model.startswith("gemini-embedding")
        return False

    def count_tokens(self, text: str) -> int:
        return len(self._encoder.encode(text))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RateLimitError, APIError)),
        reraise=True,
    )
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._settings.llm_provider.lower() == "demo":
            return [_DEMO_EMBEDDING[:] for _ in texts]

        async with self._semaphore:
            with _span("llm.embed") as span:
                span.set_attribute("llm.model", self._embedding_model())
                span.set_attribute("llm.input_count", len(texts))
                try:
                    kwargs: dict = {
                        "model": self._embedding_model(),
                        "input": list(texts),
                    }
                    if self._supports_dimensions():
                        kwargs["dimensions"] = self._settings.embedding_dim
                    response = await self._client.embeddings.create(**kwargs)
                    span.set_attribute("llm.tokens.total", response.usage.total_tokens if response.usage else 0)
                    return [item.embedding for item in response.data]
                except Exception as exc:
                    span.record_exception(exc)
                    if _OTEL_AVAILABLE:
                        span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR))
                    logger.error("embedding failed: %s", exc)
                    raise LLMClientError(f"embedding failed: {exc}") from exc

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RateLimitError, APIError)),
        reraise=True,
    )
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        response_format: dict | None = None,
    ) -> str:
        provider = self._settings.llm_provider.lower()

        if provider == "demo":
            return _DEMO_ANALYSIS_RESULT

        async with self._semaphore:
            with _span("llm.chat") as span:
                span.set_attribute("llm.model", self._chat_model())
                span.set_attribute("llm.message_count", len(messages))
                span.set_attribute("llm.temperature", temperature)
                try:
                    kwargs: dict = {
                        "model": self._chat_model(),
                        "messages": [m.model_dump() for m in messages],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    # Ollama는 response_format을 지원하지 않을 수 있으므로 openai/gemini 전용으로 제한
                    if response_format and provider in ("openai", "gemini"):
                        kwargs["response_format"] = response_format
                    response = await self._client.chat.completions.create(**kwargs)
                    if response.usage:
                        span.set_attribute("llm.tokens.prompt", response.usage.prompt_tokens)
                        span.set_attribute("llm.tokens.completion", response.usage.completion_tokens)
                    content = response.choices[0].message.content or ""
                    # Ollama JSON 응답: ```json ... ``` 래퍼 제거
                    if provider == "ollama" and content.startswith("```"):
                        lines = content.strip().splitlines()
                        content = "\n".join(
                            line for line in lines
                            if not line.strip().startswith("```")
                        )
                    return content
                except Exception as exc:
                    span.record_exception(exc)
                    if _OTEL_AVAILABLE:
                        span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR))
                    logger.error("chat completion failed: %s", exc)
                    raise LLMClientError(f"chat completion failed: {exc}") from exc


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
