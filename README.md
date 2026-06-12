# Analyzer-server

블로그 글 청킹·임베딩·Vector DB 저장, LLM 분석, 분석 기반 대화, HTML 크롤링 파이프라인을
제공하는 FastAPI 기반 비동기 AI 백엔드.

Spring 메인 서버는 사용자 요청·인증·비즈니스 로직과 Vector DB 유사도 검색을 담당하고,
이 서버는 청킹·임베딩·LLM 분석처럼 AI 변환이 필요한 작업만 맡는 분리 구조입니다.

> - Spring ↔ Python 통신 계약: [docs/CONTRACT.md](docs/CONTRACT.md)
> - 크롤 파이프라인 아키텍처 (두 가지 ingestion 경로): [docs/crawl-pipeline.md](docs/crawl-pipeline.md)
> - Redis Streams 자료구조 가이드: [docs/redis-streams.md](docs/redis-streams.md)
> - 내부 구현 관점 상세 설명서: [ai_logs/project.md](ai_logs/project.md)
> - 큐 시스템 선택 배경(RabbitMQ + Redis Streams 분리): [docs/adr-001-redis-streams-for-crawl-pipeline.md](docs/adr-001-redis-streams-for-crawl-pipeline.md)

---

## 시스템 컨텍스트

```
사용자 ─▶ Spring 메인 서버 ─┬─ HTTP (동기)        ─▶ analyzer-server
                            └─ RabbitMQ (비동기)  ─▶ analyzer-server (worker)

analyzer-server ─┬─▶ Postgres + pgvector  (문서/청크/분석 잡 저장)
                 ├─▶ Redis                (세션·캐시·rate limit·crawl streams)
                 ├─▶ RabbitMQ             (외부 분석 큐 + DLQ)
                 └─▶ OpenAI               (embed / chat)
```

진입 경로는 다음과 같이 분리된다.

| 경로 | 진입점 | 처리 방식 | 사용 큐/스트림 |
|---|---|---|---|
| 청킹/임베딩 | `POST /v1/documents/chunks` | 동기 HTTP | — |
| 분석(즉시) | `POST /v1/analysis` | 동기 HTTP | — |
| 분석(배치) | RabbitMQ message | 비동기 워커 | `blog.analysis` (+ DLX/DLQ) |
| 대화 | `POST /v1/conversations/messages` | 동기 HTTP | — |
| 크롤링 | `POST /v1/crawl/jobs` | 비동기 워커 | Redis Stream `crawl:jobs` (+ DLQ stream) |

> **유사도 검색**: `POST /v1/documents/similarity`는 v1.3부터 분석 서버에서 제공하지 않는다.
> Spring 메인 서버가 Vector DB를 직접 조회하고 BM25/hybrid 결합·권한·응답 조립을 담당한다.
> 검색어 임베딩은 Spring이 embedding provider를 직접 호출하거나 별도 합의 후 query embedding
> 전용 API를 추가한다. 이전 구현은 코드에 주석으로 보존되어 있다.

> **두 큐 시스템을 동시에 쓰는 이유**: 외부(Spring)와의 *공식 통신 계약*은 RabbitMQ에
> 두고, 본 서버 내부 파이프라인(크롤)은 Redis Streams로 분리한다. 자세한 결정 배경은
> [ADR-001](docs/adr-001-redis-streams-for-crawl-pipeline.md) 참고.

---

## 아키텍처

레이어드 아키텍처. 의존성은 단방향이다.

```
api ───▶ service ───▶ repository ───▶ (db / redis)
            ▲
            │
          client (OpenAI LLM, HTTP crawler, Redis 핸들)

worker ───▶ service ───▶ repository ───▶ ...
```

- **api**: FastAPI 라우터 (HTTP 진입점)
- **service**: 비즈니스 로직 (청킹, 분석, 대화)
- **repository**: 데이터 접근 (Postgres+pgvector, Redis)
- **client**: 외부 시스템 (OpenAI, HTTP crawler, Redis)
- **worker**: RabbitMQ analysis worker / Redis Streams crawl worker
- **domain**: ORM 모델 / Pydantic DTO / Enum
- **core**: 설정, 로깅, DB 세션, 예외, rate limiter, tracing, URL 보안

`service`는 FastAPI를 import하지 않아 워커에서도 동일한 인스턴스를 만들어 재사용한다.

---

## 주요 기능

### 1) 청킹 + 임베딩 저장 — `POST /v1/documents/chunks`
크롤러가 호출. 본문을 토큰 단위로 청킹(tiktoken `cl100k_base`)하고 임베딩하여
pgvector(`vector(768)`, HNSW + cosine)에 저장. `external_id`가 있으면 동일 문서를
upsert(기존 청크 삭제 후 재생성). 임베딩은 `embed:{model}:{sha256}` 키로 Redis에
30일 캐시 → 재크롤링 비용 절감.

### 2) 크롤링 작업 등록 — `POST /v1/crawl/jobs`
URL을 Redis Stream `crawl:jobs`에 등록 → `crawl_worker`가 fetch → HTML 본문 추출
→ 기존 청킹/임베딩 흐름으로 pgvector 저장.

보안 기본값(`app/core/url_security.py`):
- `http`/`https` URL만 허용
- localhost / private / link-local / metadata 주소 차단
- URL credential(`user:pass@`) 차단
- redirect 미추적, `trust_env=False`
- 응답 크기 상한(`CRAWL_MAX_RESPONSE_BYTES`, 기본 2MB)
- 도메인 단위 1초 간격 강제(Redis Hash)

### 3) 블로그 글 분석 — `POST /v1/analysis` (동기) 또는 RabbitMQ `blog.analysis`
LLM으로 요약/주요 토픽/톤/타겟 독자/개선 제안을 JSON으로 추출
(`response_format=json_object`로 강제). 잡 상태는 `analysis_jobs` 테이블에서
`pending → in_progress → completed | failed`로 추적. 실패 메시지는 DLX
(`blog.analysis.dlx`) → DLQ(`blog.analysis.dlq`)로 자동 격리. 워커는
`x-app-retry-count` 헤더로 최대 `WORKER_MAX_RETRIES`회까지 메인 큐 republish.

### 4) 분석 기반 대화 — `POST /v1/conversations/messages`
분석 결과를 system prompt로 주입한 멀티턴 대화. 세션은 Redis에 저장.

- 사용자별 chat rate limit (Lua 토큰 버킷, atomic)
- 세션 누적 토큰 한도 (`MAX_CONVERSATION_TOKENS`)
- 세션 턴 한도 (`MAX_TURNS_PER_SESSION`)
- TTL 기반 자동 만료 (`CONVERSATION_TTL_SECONDS`)

system prompt는 매 호출마다 분석 결과로 재생성하고 Redis에는 저장하지 않는다
(분석 갱신 반영 + 토큰 절약).

---

## 실행

### 로컬 개발

```bash
cp .env.example .env
# .env에 OPENAI_API_KEY 등 입력

docker compose up -d --build
```

| URL | 용도 |
|---|---|
| http://localhost:8000 | API |
| http://localhost:8000/docs | Swagger UI |
| http://localhost:8000/health/live | Liveness probe (외부 의존성 점검 X) |
| http://localhost:8000/health/ready | Readiness probe (DB+Redis+RabbitMQ 점검) |
| http://localhost:15672 | RabbitMQ Management (guest/guest) |

### DB 마이그레이션

```bash
docker compose exec api alembic upgrade head

# 모델 변경 후 새 리비전
docker compose exec api alembic revision --autogenerate -m "add foo column"
```

`migrations/001_init.sql`은 docker-compose 첫 부팅 시 부트스트랩용. 이후 스키마
변경은 **alembic으로만** 관리.

### 워커 단독 실행

```bash
python -m app.worker.analysis_worker   # RabbitMQ 분석 워커
python -m app.worker.crawl_worker      # Redis Streams 크롤 워커
```

---

## 환경변수

`.env.example` 참조. 핵심 항목만:

| Key | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | - | asyncpg URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | - | `redis://host:port/db` |
| `RABBITMQ_URL` | - | `amqp://...` |
| `OPENAI_API_KEY` | - | OpenAI API key |
| `LLM_MODEL` | `gpt-4o-mini` | 채팅/분석용 |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | 768차원 |
| `LLM_MAX_CONCURRENCY` | 20 | OpenAI 동시 호출 한도 |
| `WORKER_CONCURRENCY` | 10 | 분석 워커 prefetch |
| `WORKER_MAX_RETRIES` | 3 | 분석 DLQ 이동 전 재시도 |
| `CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS` | 800 / 100 | 청킹 파라미터 |
| `CRAWL_STREAM_NAME` | `crawl:jobs` | 크롤링 Redis Stream |
| `CRAWL_CONSUMER_GROUP` | `crawl-workers` | 크롤 worker consumer group |
| `CRAWL_DLQ_STREAM_NAME` | `crawl:jobs:dlq` | 크롤 실패 stream |
| `CRAWL_MAX_RETRIES` | 3 | 크롤 DLQ 이동 전 재시도 |
| `CRAWL_PENDING_IDLE_MS` | 60000 | XAUTOCLAIM idle 임계값 |
| `CRAWL_DOMAIN_DELAY_SECONDS` | 1 | 도메인별 최소 호출 간격 |
| `CRAWL_MAX_RESPONSE_BYTES` | 2000000 | HTML 응답 최대 크기 |
| `MAX_CONVERSATION_TOKENS` | 8000 | 세션 누적 토큰 한도 |
| `MAX_TURNS_PER_SESSION` | 30 | 세션 턴 한도 |
| `CHAT_RATE_CAPACITY` / `CHAT_RATE_REFILL_PER_SEC` | 30 / 0.2 | 사용자별 chat 한도 |
| `ANALYSIS_RATE_CAPACITY` / `ANALYSIS_RATE_REFILL_PER_SEC` | 10 / 0.05 | 사용자별 analysis 한도 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (미설정) | 설정 시 트레이싱 활성화 |

---

## Redis 키 컨벤션

| 키 패턴 | 자료구조 | 용도 |
|---|---|---|
| `chat:session:{session_id}` | List | 대화 메시지 히스토리 |
| `chat:tokens:{session_id}` | String (counter) | 세션 누적 토큰 |
| `embed:{model}:{sha256}` | String | 임베딩 캐시 (TTL 30일) |
| `ratelimit:{scope}:{user_id}` | Hash | Lua 토큰 버킷 |
| `crawl:jobs` | Stream | 크롤 작업 큐 |
| `crawl:jobs:dlq` | Stream | 크롤 영구 실패 격리 |
| `crawl:seen:urls` | Set | URL 중복 큐잉 차단 |
| `crawl:ratelimit:domain` | Hash | 도메인별 마지막 호출 시각 |

---

## 운영 고려사항

- **수평 확장**: API/워커 모두 stateless. `docker compose up --scale worker=N`
  또는 k8s replicas. crawl 워커의 consumer name은 `worker-{hostname}-{pid}`로
  자동 유니크.
- **LLM 동시성**: `LLM_MAX_CONCURRENCY` semaphore로 OpenAI 호출 제한.
- **재시도**: tenacity로 LLM transient 흡수, 워커는 DLX(MQ) 또는 stream re-add
  (Redis) 기반 메시지 재시도.
- **Stuck 메시지 회수**: Redis Streams는 `XAUTOCLAIM`(idle ≥ `CRAWL_PENDING_IDLE_MS`)
  으로 죽은 워커의 pending 메시지를 자동 인계.
- **DLQ**: `blog.analysis.dlq` / `crawl:jobs:dlq`. 운영용 admin 도구는 미구현,
  현재는 RabbitMQ Management UI / Redis CLI로 수동 처리.
- **Rate limit**: Redis Lua 토큰 버킷. 멀티 인스턴스에서 atomic.
- **임베딩 캐시**: SHA256(text)+model 키로 동일 텍스트 비용 절감.
- **트레이싱**: OTLP 엔드포인트 설정 시 자동 활성화. 미설치/미설정 시 no-op.
- **벡터 인덱스**: HNSW(cosine, m=16, ef_construction=64). 대용량 시 `ef_search`
  튜닝 또는 IVFFlat 전환.
- **인증**: 미구현. 현재 내부 네트워크 신뢰 모델. mTLS 또는 internal API key
  도입 필요(`docs/CONTRACT.md` §4).

---

## 테스트

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/unit
```

`tests/unit/`에 단위 테스트 다수 (chunker / rate limiter / embedding cache /
html extractor / url security / crawl service / crawl worker / document service /
conversation service). `tests/unit/fakes.py`의 in-memory Redis/LLM 더블로
외부 의존 없이 service까지 검증.

SQLAlchemy + pgvector가 필요한 통합 테스트는 docker-compose 환경에서 실행.

---

## 자주 만지는 곳

| 작업 | 파일 |
|---|---|
| 청킹 전략 | `app/service/chunker.py` |
| 분석 LLM 프롬프트 | `app/service/analysis_service.py` (`_ANALYSIS_SYSTEM_PROMPT`) |
| 대화 LLM 프롬프트 | `app/service/conversation_service.py` (`_CHAT_SYSTEM_PROMPT_TEMPLATE`) |
| Rate limit 정책 값 | `app/core/config.py` |
| Rate limit 알고리즘 | `app/core/rate_limiter.py` (`_LUA_TOKEN_BUCKET`) |
| 새 API 엔드포인트 | `app/api/*_router.py` + `app/api/dependencies.py` |
| 분석 워커 | `app/worker/analysis_worker.py` |
| 크롤 워커 | `app/worker/crawl_worker.py` |
| URL 보안 정책 | `app/core/url_security.py` |
| DB 스키마 변경 | `app/domain/models.py` → `alembic revision --autogenerate` |
| Spring ↔ Python 계약 | `docs/CONTRACT.md` |
