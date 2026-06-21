from __future__ import annotations

from app.client.llm_client import LLMClient
from app.core.logging import get_logger
from app.domain.enums import ChatRole, SourceType
from app.domain.schemas import ChatMessage

logger = get_logger(__name__)


_HYDE_JOB_PROMPT = """\
당신은 채용 공고를 읽고, 그 공고에 부합하는 지원자가 자신의 블로그에 작성했을 법한 \
가상의 글 한 단락을 생성하는 어시스턴트입니다.

지침:
- 공고에서 요구하는 핵심 기술/경험/도메인을 추출하세요.
- 그 경험을 1인칭 시점("나는 ~을 했다", "~프로젝트에서 ~를 적용했다")으로 풀어쓰세요.
- 3~5문장, 한국어로 작성하세요.
- 공고의 키워드를 자연스럽게 본문에 녹여 넣되, 단순 나열이 아닌 경험 서술로 표현하세요.
- 제목/머리말/JSON/리스트 없이 단락 형태의 본문만 출력하세요.
"""

_HYDE_BLOG_PROMPT = """\
당신은 외부 블로그 글의 핵심 내용을 추출해, 동일한 주제로 다른 블로거가 작성했을 법한 \
가상의 한 단락 본문을 생성하는 어시스턴트입니다.

지침:
- 글의 핵심 주제와 다루는 기술/개념/문제를 추출하세요.
- 동일 주제의 글이 가질 법한 표현으로 자연스럽게 한 단락을 작성하세요.
- 3~5문장, 한국어로 작성하세요.
- 제목/머리말/JSON/리스트 없이 단락 형태의 본문만 출력하세요.
"""


class QueryRewriter:
    """HyDE(Hypothetical Document Embedding) 전략 구현.

    공고나 외부 블로그 텍스트를 그대로 임베딩하면 블로그 본문과 문체/형식 차이로
    유사도가 낮게 나오는 경향이 있다. 가상의 답변/본문을 LLM으로 생성한 뒤
    그것을 임베딩 쿼리로 사용하면 매칭 품질이 개선된다.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def rewrite(self, source_type: SourceType, text: str) -> str:
        prompt = self._select_prompt(source_type)
        if prompt is None:
            return text

        truncated = text[:6000]
        try:
            rewritten = await self._llm.chat(
                messages=[
                    ChatMessage(role=ChatRole.SYSTEM, content=prompt),
                    ChatMessage(role=ChatRole.USER, content=truncated),
                ],
                temperature=0.4,
                max_tokens=400,
            )
            rewritten = rewritten.strip()
            if not rewritten:
                return text
            return rewritten
        except Exception as exc:
            # HyDE 실패 시 원문으로 fallback. 검색은 계속 가능해야 한다.
            logger.warning("HyDE rewrite failed, fallback to original: %s", exc)
            return text

    @staticmethod
    def _select_prompt(source_type: SourceType) -> str | None:
        if source_type == SourceType.JOB_POSTING:
            return _HYDE_JOB_PROMPT
        if source_type == SourceType.EXT_BLOG:
            return _HYDE_BLOG_PROMPT
        return None  # MY_BLOG 등은 변환하지 않음
