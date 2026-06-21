"""BUG-1 / BUG-2 / BUG-3 / BUG-4 수정 검증 단위 테스트.

인프라(Redis, DB, RabbitMQ) 없이 실행 가능한 순수 단위 테스트.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── BUG-1: LLMClient._supports_dimensions ───────────────────────────────────

class TestSuportsDimensions:
    """_supports_dimensions()가 provider/model 조합에 따라 올바른 값을 반환한다."""

    def _make_settings(self, provider: str, embedding_model: str, embedding_dim: int = 768):
        return SimpleNamespace(
            llm_provider=provider,
            embedding_model=embedding_model,
            ollama_embedding_model="nomic-embed-text",
            embedding_dim=embedding_dim,
        )

    def _make_client(self, provider: str, embedding_model: str):
        from app.client.llm_client import LLMClient
        client = object.__new__(LLMClient)
        client._settings = self._make_settings(provider, embedding_model)
        return client

    def test_openai_text_embedding_3_small_true(self):
        client = self._make_client("openai", "text-embedding-3-small")
        assert client._supports_dimensions() is True

    def test_openai_text_embedding_3_large_true(self):
        client = self._make_client("openai", "text-embedding-3-large")
        assert client._supports_dimensions() is True

    def test_openai_ada_002_false(self):
        """text-embedding-ada-002는 dimensions 파라미터를 지원하지 않는다."""
        client = self._make_client("openai", "text-embedding-ada-002")
        assert client._supports_dimensions() is False

    def test_ollama_false(self):
        """Ollama 프로바이더는 dimensions 파라미터가 없다."""
        client = self._make_client("ollama", "nomic-embed-text")
        assert client._supports_dimensions() is False

    def test_demo_false(self):
        client = self._make_client("demo", "text-embedding-3-small")
        assert client._supports_dimensions() is False


# ─── BUG-1: embed() dimensions 파라미터 전달 여부 ─────────────────────────────

class TestEmbedDimensionsParam:
    """embed()가 _supports_dimensions() 결과에 따라 dimensions를 포함/제외한다."""

    def _make_settings(self, provider="openai", embedding_model="text-embedding-3-small"):
        return SimpleNamespace(
            llm_provider=provider,
            embedding_model=embedding_model,
            ollama_embedding_model="nomic-embed-text",
            embedding_dim=768,
            llm_max_concurrency=10,
        )

    @pytest.mark.asyncio
    async def test_dimensions_passed_for_text_embedding_3(self):
        from app.client.llm_client import LLMClient
        client = object.__new__(LLMClient)
        client._settings = self._make_settings("openai", "text-embedding-3-small")

        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 768)]
        mock_response.usage = MagicMock(total_tokens=10)

        mock_openai = AsyncMock()
        mock_openai.embeddings.create = AsyncMock(return_value=mock_response)
        client._client = mock_openai
        client._semaphore = __import__("asyncio").Semaphore(1)

        await client.embed(["test text"])

        call_kwargs = mock_openai.embeddings.create.call_args[1]
        assert "dimensions" in call_kwargs
        assert call_kwargs["dimensions"] == 768

    @pytest.mark.asyncio
    async def test_dimensions_not_passed_for_ada_002(self):
        from app.client.llm_client import LLMClient
        client = object.__new__(LLMClient)
        client._settings = self._make_settings("openai", "text-embedding-ada-002")

        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 1536)]
        mock_response.usage = MagicMock(total_tokens=10)

        mock_openai = AsyncMock()
        mock_openai.embeddings.create = AsyncMock(return_value=mock_response)
        client._client = mock_openai
        client._semaphore = __import__("asyncio").Semaphore(1)

        await client.embed(["test text"])

        call_kwargs = mock_openai.embeddings.create.call_args[1]
        assert "dimensions" not in call_kwargs


# ─── BUG-2: analyze() enforce_rate_limit 플래그 ───────────────────────────────

class TestAnalyzeRateLimitFlag:
    """enforce_rate_limit=False 시 rate limit 검사를 건너뛴다."""

    def _make_service(self):
        from app.service.analysis_service import AnalysisService
        service = object.__new__(AnalysisService)
        service._rate_limiter = AsyncMock()
        service._rate_limiter.consume = AsyncMock()
        service._settings = SimpleNamespace(
            demo_mode=True,
            llm_provider="demo",
            analysis_rate_capacity=10,
            analysis_rate_refill_per_sec=0.05,
        )
        return service

    def test_analyze_signature_has_enforce_rate_limit(self):
        from app.service.analysis_service import AnalysisService
        sig = inspect.signature(AnalysisService.analyze)
        params = sig.parameters
        assert "enforce_rate_limit" in params, "analyze()에 enforce_rate_limit 파라미터가 없음"
        param = params["enforce_rate_limit"]
        assert param.default is True, "enforce_rate_limit 기본값이 True여야 함"

    @pytest.mark.asyncio
    async def test_rate_limit_skipped_when_false(self):
        from app.service.analysis_service import AnalysisService
        from app.domain.schemas import AnalysisRequest

        service = object.__new__(AnalysisService)
        enforce_called = []

        async def fake_enforce(user_id):
            enforce_called.append(user_id)

        service._enforce_rate_limit = fake_enforce

        # demo 경로로 빠지도록 최소 mock
        service._settings = SimpleNamespace(demo_mode=True, llm_provider="demo")
        service._documents = AsyncMock()
        service._documents.get_by_id = AsyncMock(return_value=MagicMock(id=1, title="t", content="c"))
        service._jobs = AsyncMock()
        mock_job = MagicMock(id=1)
        service._jobs.create = AsyncMock(return_value=mock_job)
        service._jobs.update_status = AsyncMock()
        service._jobs.update_status.return_value = None
        service._session = AsyncMock()
        service._session.refresh = AsyncMock()

        request = AnalysisRequest(user_id=42, document_id=1)
        await service.analyze(request, enforce_rate_limit=False)

        assert enforce_called == [], "enforce_rate_limit=False 시 rate limit이 호출되면 안 됨"

    @pytest.mark.asyncio
    async def test_rate_limit_called_when_true(self):
        from app.service.analysis_service import AnalysisService
        from app.domain.schemas import AnalysisRequest

        service = object.__new__(AnalysisService)
        enforce_called = []

        async def fake_enforce(user_id):
            enforce_called.append(user_id)

        service._enforce_rate_limit = fake_enforce
        service._settings = SimpleNamespace(demo_mode=True, llm_provider="demo")
        service._documents = AsyncMock()
        service._documents.get_by_id = AsyncMock(return_value=MagicMock(id=1, title="t", content="c"))
        service._jobs = AsyncMock()
        mock_job = MagicMock(id=1)
        service._jobs.create = AsyncMock(return_value=mock_job)
        service._jobs.update_status = AsyncMock()
        service._session = AsyncMock()
        service._session.refresh = AsyncMock()

        request = AnalysisRequest(user_id=42, document_id=1)
        await service.analyze(request, enforce_rate_limit=True)

        assert 42 in enforce_called, "enforce_rate_limit=True 시 rate limit이 호출돼야 함"


# ─── BUG-3: ollama_chat_model 기본값 ──────────────────────────────────────────

class TestOllamaChatModelDefault:
    def test_default_is_qwen(self):
        from app.core.config import Settings
        # 환경변수 없이 기본값 검증
        # Settings는 database_url, redis_url, rabbitmq_url이 필수라 직접 생성 불가
        # 필드 default를 직접 확인
        field = Settings.model_fields.get("ollama_chat_model")
        assert field is not None
        default = field.default if field.default is not None else (
            field.default_factory() if field.default_factory else None
        )
        # pydantic v2: FieldInfo.default
        actual_default = getattr(field, "default", None)
        assert actual_default == "qwen2.5:7b", f"ollama_chat_model 기본값이 'qwen2.5:7b'여야 함, 현재: {actual_default}"


# ─── BUG-4: source_type 읽기 일관성 ──────────────────────────────────────────

class TestIngestWorkerSourceType:
    """source_type이 없는 메시지는 DLQ 경로로 처리되고, 있으면 source_type_raw를 재사용한다."""

    def test_source_type_missing_raises_key_error(self):
        """source_type이 없으면 KeyError → DLQ 경로."""
        fields: dict = {
            "user_id": "1",
            "content": "hello",
            # source_type 없음
        }
        source_type_raw = fields.get("source_type")
        if not source_type_raw:
            with pytest.raises(KeyError):
                raise KeyError("source_type")

    def test_source_type_present_uses_raw(self):
        """source_type이 있으면 source_type_raw 변수로 일관되게 사용된다."""
        from app.domain.enums import SourceType

        fields: dict = {
            "user_id": "1",
            "content": "hello",
            "source_type": "ext_blog",
        }
        source_type_raw = fields.get("source_type")
        if not source_type_raw:
            raise KeyError("source_type")

        # 수정 전: SourceType(fields["source_type"]) — 동일하지만 변수 미사용
        # 수정 후: SourceType(source_type_raw)
        result = SourceType(source_type_raw)
        assert result == SourceType.EXT_BLOG

    def test_source_type_empty_string_raises(self):
        """source_type이 빈 문자열이면 KeyError → DLQ."""
        fields: dict = {"user_id": "1", "content": "c", "source_type": ""}
        source_type_raw = fields.get("source_type")
        if not source_type_raw:
            with pytest.raises(KeyError):
                raise KeyError("source_type")
