# Analyzer-server

블로그 글 청킹/임베딩, LLM 분석, 분석 기반 대화형 기능을 제공하는 FastAPI 기반 AI 백엔드.

> Spring 메인 서버와의 통신 계약은 [docs/CONTRACT.md](docs/CONTRACT.md) 참고.

## 아키텍처

레이어드 아키텍처로 구성:

```
api → service → repository → (db / external)
                    ↑
                  client (LLM, Redis)
worker → service → repository → ...
```

- **api**: FastAPI 라우터 (HTTP 진입점)
- **service**: 비즈니스 로직 (청킹, 분석, 대화)
- **repository**: 데이터 접근 (Postgres+pgvector, Redis)
- **client**: 외부 시스템 (OpenAI, Redis)
- **worker**: RabbitMQ consumer (분석 잡 처리)
- **domain**: 모델/스키마/enum
- **core**: 설정, 예외, 로깅, DB 세션, rate limiter, tracing

## 주요 기능

### 1) 청킹 + 임베딩 저장
`POST /v1/documents/chunks`

크롤러가 호출. 본문을 토큰 단위로 청킹하고 임베딩하여 pgvector에 저장.
`external_id`가 있으면 동일 문서 갱신(기존 청크 삭제 후 재생성).
임베딩은 SHA256 기반 Redis 캐시로 비용 절감.

### 2) 유사도 매칭
`POST /v1/documents/similarity`

외부 블로그/공고 텍스트를 입력으로 본인 블로그 글 중 유사한 글을 검색.

- `use_hyde=true` — 공고/외부 블로그를 LLM으로 가상 본문 변환 후 임베딩 (벡터 정확도↑)
- `use_hybrid=true` — pgvector + BM25(tsvector) RRF 결합 (키워드 매칭↑)
- 둘 다 켜면: HyDE는 임베딩 쿼리에만, BM25는 원본 키워드 사용

청크 단위로 검색하되 document 단위로 max score 집계.

### 3) 블로그 글 분석
`POST /v1/analysis` (동기) 또는 RabbitMQ `blog.analysis` 큐 (비동기)

LLM으로 요약, 주요 토픽, 톤, 타겟 독자, 개선 제안을 JSON으로 추출.
실패 메시지는 DLX → `blog.analysis.dlq` 로 자동 격리.

### 4) 분석 기반 대화
`POST /v1/conversations/messages`

분석 결과를 컨텍스트로 사용자와 대화. 세션은 Redis에 저장.
- 세션당 토큰 한도 (`MAX_CONVERSATION_TOKENS`)
- 세션당 턴 한도 (`MAX_TURNS_PER_SESSION`)
- 사용자별 rate limit (token bucket, Redis Lua)
- TTL 기반 자동 만료

## 실행

### 로컬 개발
```bash
cp .env.example .env
# .env에 OPENAI_API_KEY 등 입력

docker compose up -d --build
```

- API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- Liveness: http://localhost:8000/health/live
- Readiness: http://localhost:8000/health/ready
- RabbitMQ Management: http://localhost:15672 (guest/guest)

### DB 마이그레이션
```bash
# 컨테이너 안에서
alembic upgrade head
```

docker-compose는 첫 기동 시 `migrations/001_init.sql`로 부트스트랩하지만,
이후 스키마 변경은 alembic으로만 관리할 것.

### 워커 단독 실행
```bash
python -m app.worker.analysis_worker
```

## 환경변수

| Key | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | - | asyncpg URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | - | `redis://host:port/db` |
| `RABBITMQ_URL` | - | `amqp://...` |
| `OPENAI_API_KEY` | - | OpenAI API key |
| `LLM_MODEL` | `gpt-4o-mini` | 채팅/분석용 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 1536차원 |
| `LLM_MAX_CONCURRENCY` | 20 | OpenAI 동시 호출 한도 |
| `WORKER_CONCURRENCY` | 10 | 워커 prefetch |
| `WORKER_MAX_RETRIES` | 3 | DLQ 보내기 전 재시도 횟수 |
| `CHAT_RATE_CAPACITY` / `CHAT_RATE_REFILL_PER_SEC` | 30 / 0.2 | 사용자별 chat 한도 |
| `ANALYSIS_RATE_CAPACITY` / `ANALYSIS_RATE_REFILL_PER_SEC` | 10 / 0.05 | 사용자별 analysis 한도 |
| `MAX_CONVERSATION_TOKENS` | 8000 | 세션 토큰 한도 |
| `MAX_TURNS_PER_SESSION` | 30 | 세션 턴 한도 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (미설정) | 설정 시 트레이싱 활성화 |

## 운영 고려사항

- **수평 확장**: API/워커 모두 stateless. `docker compose up --scale worker=N`
- **LLM 동시성**: `LLM_MAX_CONCURRENCY` semaphore로 OpenAI 호출 제한
- **재시도**: tenacity로 transient 오류 흡수, 워커는 DLX 기반 메시지 재시도
- **DLQ**: 영구 실패는 `blog.analysis.dlq`에 적재. UI/알람 운영 권장
- **Rate limit**: Redis Lua 토큰 버킷, 멀티 인스턴스에서 안전
- **임베딩 캐시**: SHA256(text)+model 키, 동일 텍스트 비용 절감
- **트레이싱**: OTLP 엔드포인트 설정 시 자동 활성화
- **벡터 인덱스**: HNSW (cosine). 대용량 시 `ef_search` 튜닝
- **Hybrid 검색**: tsvector는 `simple` config. 한국어 정밀도 필요 시 mecab/pg_bigm 추가

## 테스트

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/unit
```

현재 단위 테스트 23개 (chunker, query rewriter, rate limiter, embedding cache).
SQLAlchemy/pgvector가 필요한 service 통합 테스트는 testcontainers 또는
docker-compose 환경에서 별도 실행.
