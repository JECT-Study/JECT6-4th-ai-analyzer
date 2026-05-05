import asyncio
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

# OpenTelemetry는 선택적. 미설치 환경(테스트 등)에서도 import 가능하도록.
try:
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer(__name__)
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _otel_trace = None  # type: ignore[assignment]
    _tracer = None
    _OTEL_AVAILABLE = False

logger = get_logger(__name__)


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def set_attribute(self, *_args, **_kwargs):
        pass

    def record_exception(self, *_args, **_kwargs):
        pass

    def set_status(self, *_args, **_kwargs):
        pass


def _span(name: str):
    if _OTEL_AVAILABLE and _tracer is not None:
        return _tracer.start_as_current_span(name)
    return _NullSpan()


class LLMClient:
    """OpenAI 기반 LLM/임베딩 클라이언트.

    - 동시성 제한: settings.llm_max_concurrency
    - 재시도: rate limit / transient API error
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = AsyncOpenAI(api_key=self._settings.openai_api_key)
        self._semaphore = asyncio.Semaphore(self._settings.llm_max_concurrency)
        self._encoder = tiktoken.get_encoding("cl100k_base")

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
        async with self._semaphore:
            with _span("llm.embed") as span:
                span.set_attribute("llm.model", self._settings.embedding_model)
                span.set_attribute("llm.input_count", len(texts))
                try:
                    response = await self._client.embeddings.create(
                        model=self._settings.embedding_model,
                        input=list(texts),
                    )
                    span.set_attribute(
                        "llm.tokens.total", response.usage.total_tokens
                    )
                    return [item.embedding for item in response.data]
                except Exception as exc:
                    span.record_exception(exc)
                    if _OTEL_AVAILABLE:
                        span.set_status(
                            _otel_trace.Status(_otel_trace.StatusCode.ERROR)
                        )
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
        async with self._semaphore:
            with _span("llm.chat") as span:
                span.set_attribute("llm.model", self._settings.llm_model)
                span.set_attribute("llm.message_count", len(messages))
                span.set_attribute("llm.temperature", temperature)
                try:
                    kwargs = {
                        "model": self._settings.llm_model,
                        "messages": [m.model_dump() for m in messages],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    if response_format:
                        kwargs["response_format"] = response_format
                    response = await self._client.chat.completions.create(**kwargs)
                    if response.usage:
                        span.set_attribute(
                            "llm.tokens.prompt", response.usage.prompt_tokens
                        )
                        span.set_attribute(
                            "llm.tokens.completion", response.usage.completion_tokens
                        )
                    return response.choices[0].message.content or ""
                except Exception as exc:
                    span.record_exception(exc)
                    if _OTEL_AVAILABLE:
                        span.set_status(
                            _otel_trace.Status(_otel_trace.StatusCode.ERROR)
                        )
                    logger.error("chat completion failed: %s", exc)
                    raise LLMClientError(f"chat completion failed: {exc}") from exc


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
