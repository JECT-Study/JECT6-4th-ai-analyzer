from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """애플리케이션 전역 설정. 환경변수에서 로드."""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # App
    app_name: str = "blog-ai-server"
    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")

    # Database
    database_url: str
    db_pool_size: int = 20
    db_max_overflow: int = 10

    # Redis (대화 세션 캐시)
    redis_url: str
    conversation_ttl_seconds: int = 3600  # 1시간

    # Message Queue
    rabbitmq_url: str
    analysis_queue_name: str = "blog.analysis"
    analysis_dlx_name: str = "blog.analysis.dlx"
    analysis_dlq_name: str = "blog.analysis.dlq"
    worker_concurrency: int = 10
    worker_max_retries: int = 3

    # LLM
    openai_api_key: str
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    llm_max_concurrency: int = 20  # 동시 LLM 호출 제한

    # Chunking
    chunk_size_tokens: int = 800
    chunk_overlap_tokens: int = 100

    # Conversation
    max_conversation_tokens: int = 8000  # 누적 컨텍스트 한도
    max_turns_per_session: int = 30

    # Rate limiting (per user)
    chat_rate_capacity: int = 30          # burst
    chat_rate_refill_per_sec: float = 0.2  # 분당 12회
    analysis_rate_capacity: int = 10
    analysis_rate_refill_per_sec: float = 0.05  # 분당 3회


@lru_cache
def get_settings() -> Settings:
    return Settings()
