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
    llm_provider: str = Field(default="openai")  # openai | ollama | demo
    openai_api_key: str = Field(default="")
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    llm_max_concurrency: int = 20

    # Ollama (llm_provider=ollama 시 사용)
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_chat_model: str = Field(default="qwen2.5:7b")
    ollama_embedding_model: str = Field(default="nomic-embed-text")

    # Demo mode: LLM 호출 없이 고정 결과 반환
    demo_mode: bool = Field(default=False)

    # Chunking
    chunk_size_tokens: int = 800
    chunk_overlap_tokens: int = 100

    # Crawling
    crawl_stream_name: str = "crawl:jobs"
    crawl_consumer_group: str = "crawl-workers"
    crawl_dlq_stream_name: str = "crawl:jobs:dlq"
    crawl_worker_name: str = "worker-1"
    crawl_batch_size: int = 10
    crawl_block_ms: int = 5000
    crawl_pending_idle_ms: int = 60000
    crawl_max_retries: int = 3
    crawl_request_timeout_seconds: float = 15.0
    crawl_user_agent: str = "blog-ai-crawler/1.0"
    crawl_domain_delay_seconds: float = 1.0
    crawl_max_response_bytes: int = 2_000_000

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
