from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.enums import SourceType
from app.domain.schemas import ChunkRequest
from app.service.document_service import DocumentService


@pytest.fixture
def llm_mock():
    mock = AsyncMock()
    # 호출된 텍스트 수만큼 더미 임베딩 반환
    mock.embed = AsyncMock(side_effect=lambda texts: [[0.0] * 1536 for _ in texts])
    mock.chat = AsyncMock(return_value="가상 본문")
    return mock


@pytest.fixture
def session_mock():
    return MagicMock()


@pytest.fixture
def embedding_cache_mock():
    """기본은 모두 cache miss → LLM 호출로 fallback."""
    cache = AsyncMock()
    cache.get_many = AsyncMock(side_effect=lambda texts: ([None] * len(texts), list(range(len(texts)))))
    cache.set_many = AsyncMock()
    return cache


@pytest.fixture
def service(session_mock, llm_mock, embedding_cache_mock):
    svc = DocumentService(session_mock, llm_mock, embedding_cache=embedding_cache_mock)
    # repository는 통째로 mock으로 교체
    svc._documents = AsyncMock()
    # NOTE(2026-05-13): 유사도 검색은 Spring 책임으로 이동해 Python 서비스에서
    # 비활성화했다. 이전 테스트는 아래 주석으로 보존한다.
    # svc._similarity = AsyncMock()
    return svc


class TestDocumentServiceIngest:
    async def test_creates_new_document_and_chunks(self, service, llm_mock):
        # external_id 없는 신규 문서
        new_doc = MagicMock(id=42)
        service._documents.find_by_external_id = AsyncMock(return_value=None)
        service._documents.create = AsyncMock(return_value=new_doc)
        service._documents.add_chunks = AsyncMock()

        request = ChunkRequest(
            user_id=1,
            source_type=SourceType.MY_BLOG,
            title="제목",
            content="첫 문단입니다.\n\n두 번째 문단입니다.\n\n세 번째 문단도 있습니다.",
        )

        response = await service.ingest_and_chunk(request)

        assert response.document_id == 42
        assert response.chunk_count >= 1
        service._documents.create.assert_awaited_once()
        # 임베딩은 청크 수와 일치하게 한 번 batch 호출
        llm_mock.embed.assert_awaited_once()
        embed_arg = llm_mock.embed.await_args.args[0]
        assert len(embed_arg) == response.chunk_count

    async def test_upserts_when_external_id_exists(self, service):
        existing = MagicMock(id=99)
        service._documents.find_by_external_id = AsyncMock(return_value=existing)
        service._documents.delete_chunks_by_document = AsyncMock()
        service._documents.create = AsyncMock()  # 호출되면 안 됨
        service._documents.add_chunks = AsyncMock()

        request = ChunkRequest(
            user_id=1,
            source_type=SourceType.EXT_BLOG,
            external_id="ext-1",
            title="새 제목",
            content="갱신된 내용입니다.",
        )

        response = await service.ingest_and_chunk(request)

        assert response.document_id == 99
        assert existing.title == "새 제목"
        service._documents.delete_chunks_by_document.assert_awaited_once_with(99)
        service._documents.create.assert_not_awaited()


# NOTE(2026-05-13): 유사도 검색은 Spring 메인 서버가 Vector DB를 직접 조회하는
# 책임으로 이동했다. 이전 DocumentService 유사도 테스트는 이력 보존을 위해
# 주석으로 남긴다.
#
# class TestDocumentServiceSimilarity:
#     async def test_returns_matches_without_hyde(self, service, llm_mock):
#         service._similarity.search_similar_documents = AsyncMock(
#             return_value=[
#                 SimilarityHit(
#                     document_id=1,
#                     title="블로그 글 A",
#                     url="https://example.com/a",
#                     score=0.91,
#                     chunk_preview="미리보기...",
#                 )
#             ]
#         )
#
#         request = SimilarityMatchRequest(
#             user_id=1,
#             query_text="공고 텍스트",
#             target_source_type=SourceType.MY_BLOG,
#             top_k=5,
#         )
#         response = await service.find_similar(request)
#
#         assert len(response.matches) == 1
#         assert response.matches[0].score == 0.91
#         assert response.rewritten_query is None
#         # HyDE 미사용 → chat 호출 안 함
#         llm_mock.chat.assert_not_awaited()
#
#     async def test_uses_hyde_when_enabled(self, service, llm_mock):
#         service._similarity.search_similar_documents = AsyncMock(return_value=[])
#
#         request = SimilarityMatchRequest(
#             user_id=1,
#             query_text="원본 공고 텍스트",
#             target_source_type=SourceType.MY_BLOG,
#             query_source_type=SourceType.JOB_POSTING,
#             use_hyde=True,
#             top_k=5,
#         )
#         response = await service.find_similar(request)
#
#         # HyDE가 호출되어 변환된 쿼리가 응답에 포함
#         llm_mock.chat.assert_awaited_once()
#         assert response.rewritten_query == "가상 본문"
#         # 임베딩은 변환된 쿼리로 호출
#         llm_mock.embed.assert_awaited_once_with(["가상 본문"])
#
#     async def test_hyde_falls_back_when_rewritten_equals_original(
#         self, service, llm_mock
#     ):
#         # LLM이 원문과 동일한 텍스트를 돌려준 비정상 케이스
#         llm_mock.chat = AsyncMock(return_value="원본")
#         service._similarity.search_similar_documents = AsyncMock(return_value=[])
#
#         request = SimilarityMatchRequest(
#             user_id=1,
#             query_text="원본",
#             target_source_type=SourceType.MY_BLOG,
#             query_source_type=SourceType.JOB_POSTING,
#             use_hyde=True,
#         )
#         response = await service.find_similar(request)
#         assert response.rewritten_query is None
#
#     async def test_hybrid_search_uses_keywords_separate_from_hyde(
#         self, service, llm_mock
#     ):
#         """HyDE가 켜져 있어도 BM25는 원본 키워드를 사용해야 함."""
#         service._similarity.hybrid_search = AsyncMock(return_value=[])
#         llm_mock.chat = AsyncMock(return_value="가상으로 작성된 본문")
#
#         request = SimilarityMatchRequest(
#             user_id=1,
#             query_text="Kafka 기반 백엔드 채용",
#             target_source_type=SourceType.MY_BLOG,
#             query_source_type=SourceType.JOB_POSTING,
#             use_hyde=True,
#             use_hybrid=True,
#         )
#         response = await service.find_similar(request)
#
#         # 임베딩은 HyDE 변환된 텍스트로
#         llm_mock.embed.assert_awaited_once_with(["가상으로 작성된 본문"])
#         # BM25 키워드는 원본
#         called_kwargs = service._similarity.hybrid_search.await_args.kwargs
#         assert called_kwargs["keywords_query"] == "Kafka 기반 백엔드 채용"
#         assert response.rewritten_query == "가상으로 작성된 본문"
#
#     async def test_hybrid_search_uses_explicit_keywords_when_provided(
#         self, service, llm_mock
#     ):
#         service._similarity.hybrid_search = AsyncMock(return_value=[])
#         request = SimilarityMatchRequest(
#             user_id=1,
#             query_text="긴 공고 본문 ...",
#             keywords="Kafka Redis Spring",
#             target_source_type=SourceType.MY_BLOG,
#             use_hybrid=True,
#         )
#         await service.find_similar(request)
#         called_kwargs = service._similarity.hybrid_search.await_args.kwargs
#         assert called_kwargs["keywords_query"] == "Kafka Redis Spring"
