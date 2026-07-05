# Analyzer Server (ject)

블로그 본문 청킹·임베딩·pgvector 저장, LLM 기반 블로그 분석·6지표 진단·분석 기반 채팅,
온보딩 프로필 임베딩을 담당하는 **FastAPI 비동기 AI 백엔드**.

JECT 시스템에서 "AI 변환이 필요한 작업"만 맡는 서버입니다.
사용자 인증·비즈니스 로직·유사도 검색 응답 조립은 Spring 메인 API(`ject_api`)가,
웹 크롤링은 크롤러(`ject_crawl`)가 담당하는 3-서버 분리 구조입니다.

> - Spring ↔ Python 통신 계약: [docs/CONTRACT.md](docs/CONTRACT.md)
> - 크롤 파이프라인 상세: [docs/etc/crawl-pipeline.md](docs/etc/crawl-pipeline.md)
> - Redis Streams 가이드: [docs/etc/redis-streams.md](docs/etc/redis-streams.md)
> - 큐 시스템 선택 배경: [docs/adr/adr-001-redis-streams-for-crawl-pipeline.md](docs/adr/adr-001-redis-streams-for-crawl-pipeline.md)

---

## 시스템 컨텍스트

```
ject_crawl (크롤러) ──▶ Redis Stream crawl:ingest ──▶ ingest-worker ─┐
                                                                     │ 청킹·임베딩·저장
Spring API ─┬─ HTTP (동기: 진단/채팅/프로필 임베딩/결과 조회)  ──▶ analyzer-api
            └─ RabbitMQ blog.analysis (비동기 분석 요청)      ──▶ analysis-worker
                                                                     │
analysis-worker ── blog.analysis.completed ──▶ Spring API           │
                                                                     ▼
                                     Postgres + pgvector / Redis / RabbitMQ
                                     LLM Provider (Gemini 또는 Ollama)
```

하나의 코드베이스로 세 프로세스가 뜬다.

| 프로세스 | 실행 명령 | 역할 |
|---|---|---|
| `analyzer-api` | `uvicorn app.main:app` | HTTP API (진단, 채팅, 문서 청킹, 프로필 임베딩) |
| `ingest-worker` | `python -m app.worker.ingest_worker` | Redis Stream `crawl:ingest` 소비 → 청킹·임베딩·저장 → 분석 큐 발행 |
| `analysis-worker` | `python -m app.worker.analysis_worker` | RabbitMQ `blog.analysis` 소비 → LLM 분석 → 완료 이벤트 발행 |

이 외에 `alembic upgrade head`를 수행하는 one-shot `migrate` 컨테이너가 기동 순서 상 먼저 실행된다.

---

## 아키텍처

레이어드 아키텍처. 의존성은 단방향이다.

```
api ───▶ service ───▶ repository ───▶ (Postgres+pgvector / Redis)
             ▲
             │
           client (LLM provider, Redis 핸들)

worker ───▶ service ───▶ repository ───▶ ...
```

```
app/
├── main.py         FastAPI 진입점, lifespan 관리
├── api/            HTTP 라우터 — analysis / conversation / diagnosis / document / health / profile
├── service/        비즈니스 로직 — chunker, document, analysis, blog_diagnosis, blog_snapshot,
│                   conversation, profile_embedding, html_extractor, query_rewriter
├── repository/     데이터 접근 — document, analysis, conversation, influencer, similarity,
│                   embedding_cache, crawl_queue(Stream 소비)
├── client/         외부 시스템 클라이언트 (LLM, Redis)
├── domain/         ORM 모델 / Pydantic DTO / Enum
├── core/           설정(config.py), 예외, 로깅, DB 세션, rate limiter
└── worker/         ingest_worker, analysis_worker
```

- `service`는 FastAPI를 import하지 않아 API와 워커가 동일 서비스 인스턴스를 재사용한다.
- LLM Provider는 `LLM_PROVIDER` 설정으로 Gemini / Ollama를 전환한다.

### 책임에서 제외된 것

- **유사도 검색 API 없음** — `POST /v1/documents/similarity`는 제거됨(코드에 주석으로 이력 보존).
  추천/검색 조회는 Spring이 pgvector 테이블을 직접 조회한다.
- **HTTP 크롤링 없음** — 과거 내부 크롤 워커(`crawl:jobs`) 경로는 폐기.
  수집은 `ject_crawl`이 담당하고 본문은 Redis Stream `crawl:ingest`로만 유입된다.

---

## API 목록

`--root-path /analyzer`로 기동되어 게이트웨이(Nginx) 뒤에서는 `/analyzer/*` 프리픽스로 노출된다.

| Method | Path | 설명 |
|---|---|---|
| GET | `/health`, `/health/live` | 프로세스 생존 확인 (외부 의존성 점검 없음) |
| GET | `/health/ready` | DB·Redis·RabbitMQ readiness 점검 |
| POST | `/v1/documents/chunks` | 문서 청킹·임베딩·pgvector 저장. `external_id` 기반 upsert |
| POST | `/v1/analysis` | 직접 호출용 동기 분석 |
| GET | `/v1/analysis/documents/{document_id}` | 문서의 최근 분석 결과 조회 |
| POST | `/v1/diagnosis` | 6지표 블로그 진단 결과 생성 |
| POST | `/v1/conversations/messages` | 분석 결과 기반 멀티턴 채팅 |
| DELETE | `/v1/conversations/{session_id}` | Redis 채팅 세션 초기화 |
| POST | `/v1/profile/embed` | 온보딩 프로필 텍스트 임베딩 생성·저장 |

---

## 동작 방식

### 1) Ingest 파이프라인 (ingest-worker)

`ject_crawl`이 `crawl:ingest` Stream에 publish한 본문을 consumer group `ingest-workers`로 소비한다.
메시지의 `source_type`은 세 가지: `job_posting`(공고), `my_blog`(본인 블로그), `ext_blog`(인플루언서 블로그).

- 본문을 토큰 단위 청킹 → 임베딩 → `documents` / `document_chunks`에 저장
- **POST 모드**: 포스트별 문서를 저장한 뒤 곧바로 RabbitMQ `blog.analysis`에 분석 요청 발행
- **FULL_BLOG 모드**: `batch:{batchId}:*` 키(doc_ids / completed / expected)로 진행률을 추적하고,
  모든 포스트 ingest 완료 또는 deadline 초과 시 `blog_snapshot` 문서를 생성해
  snapshot `document_id` 1건만 분석 큐에 발행
- `ext_blog` 처리 시 `influencer` 테이블에 인플루언서 프로필을 upsert
- 재시도(최대 3회) 초과 시 메시지를 `crawl:ingest:dlq` Stream으로 격리

### 2) 분석 워커 (analysis-worker)

- RabbitMQ `blog.analysis` 큐 소비 (생산자: Spring `AnalysisQueuePublisher` 및 ingest-worker)
- LLM 분석 실행 → 결과·상태를 `analysis_jobs` 테이블에 저장 (`pending → in_progress → completed | failed`)
- 성공 시 `blog.analysis.completed` 이벤트 발행 → Spring `AnalysisCompletedListener`가 수신
- `x-app-retry-count` 헤더 기반 재시도 3회 초과 시 `blog.analysis.dlx` → `blog.analysis.dlq` 격리

### 3) 6지표 진단 (`POST /v1/diagnosis`)

Spring이 인증·쿼터 검증 후 동기 프록시로 호출한다(호출 측 read timeout 120초).
`BlogDiagnosisService`가 document metadata와 LLM 결과로 metrics, category_fit,
strengths, weaknesses를 생성해 `blog_diagnoses`에 저장한다.
`result_embedding`이 있으면 추천·유사도 확장에 활용할 수 있다.

### 4) 분석 기반 채팅 (`POST /v1/conversations/messages`)

- 대화 이력은 `chat:session:{session_id}`(Redis)에 TTL 기반 저장, 누적 토큰은 `chat:tokens:{session_id}`
- 한도: 누적 `MAX_CONVERSATION_TOKENS=8000` 토큰, `MAX_TURNS_PER_SESSION=30` 턴
- 사용자별 rate limit은 Redis **Lua 토큰 버킷**으로 원자 처리 (멀티 인스턴스 안전)
- system prompt는 매 호출마다 최신 분석 결과로 재생성 (Redis에 저장하지 않음)

### 5) 온보딩 프로필 임베딩 (`POST /v1/profile/embed`)

Spring이 온보딩 응답을 텍스트화해 전달하면 임베딩을 생성하고
`profile_embeddings(user_id, embedding, profile_hash)`에 저장한다.
`(user_id, profile_hash)` 유니크 제약으로 동일 프로필 중복 저장을 방지한다.

---

## 데이터 저장

| 테이블 | 역할 |
|---|---|
| `documents`, `document_chunks` | 수집 원문과 청크. `embedding`은 `vector(768)`, HNSW cosine index |
| `analysis_jobs` | LLM 분석 상태·결과 JSON. Spring이 조회·이력 연결에 사용 |
| `blog_diagnoses` | 6지표 진단 결과, 카테고리 적합도, 강점·약점, 결과 임베딩 |
| `profile_embeddings` | 온보딩 프로필 임베딩. `(user_id, profile_hash)` 유니크 |
| `influencer` | 인플루언서 프로필 (ext_blog ingest 시 upsert, 조회 주체는 Spring) |

스키마는 **Alembic으로만** 관리한다. (`alembic upgrade head`)

## Redis 키 컨벤션 (이 서버 소유)

| 키 패턴 | 자료구조 | 용도 |
|---|---|---|
| `crawl:ingest` | Stream | 크롤러 → ingest-worker 본문 인입 (group: `ingest-workers`) |
| `crawl:ingest:dlq` | Stream | ingest 재시도 초과 메시지 격리 |
| `batch:{batchId}:*` | String/Set/Counter | FULL_BLOG 포스트 ingest 진행률·deadline·snapshot 추적 |
| `chat:session:{session_id}` | List | 대화 메시지 히스토리 (TTL) |
| `chat:tokens:{session_id}` | String | 세션 누적 토큰 |
| `embed:{model}:{digest}` | String | 동일 텍스트(SHA-256) 임베딩 캐시 |
| `ratelimit:{scope}:{user_id}` | Hash | Lua 토큰 버킷 |

## RabbitMQ 토폴로지

| 큐/Exchange | 생산자 | 소비자 |
|---|---|---|
| `blog.analysis` | Spring, ingest-worker | analysis-worker |
| `blog.analysis.dlx` / `blog.analysis.dlq` | RabbitMQ DLX | (수동 확인) |
| `blog.analysis.completed` | analysis-worker | Spring `AnalysisCompletedListener` |

---

## 실행·테스트

```bash
# 마이그레이션
alembic upgrade head

# API / 워커
uvicorn app.main:app --port 8000
python -m app.worker.ingest_worker
python -m app.worker.analysis_worker

# 단위 테스트
pytest tests/unit
```

환경변수는 `app/core/config.py`가 source of truth다.
핵심: `DATABASE_URL`(asyncpg), `REDIS_URL`, `RABBITMQ_URL`, `LLM_PROVIDER`(gemini|ollama)와
각 provider의 모델/엔드포인트 설정. 통합 기동은 `ject_integration_data/docker-compose.yml` 참조.
