# CLAUDE.md

이 문서는 본 프로젝트(blog-ai-server)에 대한 전반적인 설명서입니다.
새로 합류하는 개발자나 AI 어시스턴트가 빠르게 맥락을 잡을 수 있도록 작성됐습니다.

---

## 1. 프로젝트 한 줄 요약

**블로그 글을 LLM으로 분석·임베딩하여, 외부 글/공고와의 유사도 매칭과 분석 기반 대화를 제공하는 Python AI 백엔드.**

Spring 메인 서버는 사용자 요청·인증·비즈니스 로직을 담당하고, AI/벡터 처리는
이 서버에 위임하는 분리 구조입니다.

---

## 2. 시스템 컨텍스트

```
┌─────────────────┐         ┌────────────────────┐
│  사용자 (Web/App) │ ──────▶ │  Spring 메인 서버   │
└─────────────────┘         └─────────┬──────────┘
                                      │
                          HTTP        │       MQ publish
                          (동기)       │       (비동기)
                                      ▼
                          ┌──────────────────────────┐
                          │  blog-ai-server (Python) │
                          │  - 청킹/임베딩            │
                          │  - 유사도 검색           │
                          │  - LLM 분석              │
                          │  - 대화                  │
                          └──────────┬───────────────┘
                                     │
                  ┌──────────────────┼─────────────────┐
                  ▼                  ▼                 ▼
            ┌──────────┐       ┌──────────┐     ┌──────────┐
            │ Postgres │       │  Redis   │     │ RabbitMQ │
            │ +pgvector│       │ (cache,  │     │ (queue)  │
            │          │       │  rate    │     │          │
            │          │       │  limit)  │     │          │
            └──────────┘       └──────────┘     └──────────┘
                                     │
                                     ▼
                              ┌─────────────┐
                              │  OpenAI API │
                              │ (embed,chat)│
                              └─────────────┘
```

- **크롤러 (Spring 측)**: 블로그/공고를 수집한 뒤 `POST /v1/documents/chunks`로 위임
- **분석 트리거 (Spring 측)**: RabbitMQ `blog.analysis` 큐에 `{user_id, document_id}` publish
- **사용자 대화 (Spring 측)**: HTTP로 `/v1/conversations/messages` 프록시 호출

자세한 인터페이스 계약은 [`docs/CONTRACT.md`](docs/CONTRACT.md) 참조.

---

## 3. 디렉토리 구조와 책임

```
blog-ai-server/
├── app/
│   ├── main.py               FastAPI 앱 진입점, lifespan 관리
│   ├── api/                  HTTP 라우터 (Controller)
│   ├── service/              비즈니스 로직
│   ├── repository/           DB/캐시 접근
│   ├── client/               외부 시스템 클라이언트 (OpenAI, Redis)
│   ├── domain/               모델 / DTO / Enum
│   ├── core/                 설정, 예외, 로깅, DB, 트레이싱, rate limit
│   └── worker/               RabbitMQ consumer
├── alembic/                  DB 마이그레이션 (async + pgvector)
├── docs/
│   └── CONTRACT.md           Spring ↔ Python 통신 계약 문서
├── migrations/
│   └── 001_init.sql          docker-compose 부트스트랩용 raw SQL
├── tests/unit/               단위 테스트 (mock 기반)
├── Dockerfile
├── docker-compose.yml
└── requirements*.txt
```

### 레이어드 아키텍처 의존성 방향

```
api ─▶ service ─▶ repository ─▶ (db / external)
                       ▲
                       │
                     client (LLM, Redis)

worker ─▶ service ─▶ repository ─▶ ...
```

- **단방향**: 상위 레이어만 하위 레이어를 알 수 있음
- **service는 FastAPI를 import하지 않음** (API 외 워커에서도 재사용)
- **repository는 service 로직을 모름**
- **DTO(Pydantic)와 ORM 모델 분리**: domain/schemas.py vs domain/models.py

---

## 4. 핵심 기능

### 4.1 청킹 + 임베딩 (`POST /v1/documents/chunks`)

크롤러가 글을 수집한 뒤 호출. 본문을 토큰 단위로 청킹 → OpenAI 임베딩 → pgvector 저장.

- **청킹 전략**: 문단(`\n\n`) 우선 → 너무 긴 문단은 토큰 슬라이싱 → overlap 적용
- **upsert 지원**: `external_id`가 있으면 기존 문서의 청크 삭제 후 재생성 (재크롤링 대응)
- **임베딩 캐시**: `SHA256(text) + model name`을 키로 Redis에 30일 저장 (재크롤링 비용 절감)

### 4.2 유사도 검색 (`POST /v1/documents/similarity`)

외부 블로그/공고 텍스트로 본인 블로그 글을 검색.

**3가지 검색 모드를 조합 가능:**

1. **기본 벡터 검색**: cosine similarity (pgvector HNSW 인덱스)
2. **HyDE** (`use_hyde=true`): 공고를 "이런 경험을 한 사람" 가상 본문으로 LLM 변환 → 그것을 임베딩
   - *왜?* 공고는 "요구함"이 본질이고 블로그는 "내가 했음"이 본질이라 문체가 다름. 변환하면 매칭이 살아남
3. **Hybrid** (`use_hybrid=true`): 벡터 + BM25(`tsvector`) RRF 결합
   - *왜?* 벡터는 "Kafka"·"Kafka 같은 시스템"을 비슷하게 보지만, 정확히 "Kafka"를 명시한 글이 더 가치있을 수 있음

**중요**: HyDE와 Hybrid를 동시에 켜면, **HyDE는 임베딩 쿼리에만, BM25는 원본 키워드를 사용**.
HyDE 변환문에서는 원본의 고유명사(스킬명 등)가 옅어지기 때문.

**집계**: 청크 단위로 검색 → `DISTINCT ON document_id`로 문서 단위 max score 집계.

### 4.3 블로그 글 분석 (큐 또는 `POST /v1/analysis`)

LLM으로 글을 JSON 구조로 분석:

```json
{
  "summary": "...",
  "key_topics": ["..."],
  "tone": "...",
  "target_audience": "...",
  "suggestions": ["..."]
}
```

- **비동기 우선**: Spring이 `blog.analysis` 큐에 publish → 워커가 처리
- **결과는 `analysis_jobs` 테이블에 저장**: status (pending/in_progress/completed/failed) 추적
- **재시도**: 워커 내부 tenacity(LLM transient) + 큐 republish(`x-app-retry-count` 헤더, 기본 3회)
- **DLQ**: 재시도 초과 시 `blog.analysis.dlx` → `blog.analysis.dlq`로 격리

### 4.4 대화 (`POST /v1/conversations/messages`)

분석 결과를 system prompt에 주입한 멀티턴 대화.

- **세션 저장**: Redis (TTL 1시간 기본)
- **삼중 한도**: 세션 토큰 / 세션 턴 / 사용자별 rate limit
- **stateless**: 서버는 세션 키만으로 컨텍스트 복원, 멀티 인스턴스 안전

---

## 5. 주요 설계 결정 (Why)

### 5.1 왜 Python인가?

기존에 LLM API 호출용 Python 서버가 이미 있었고, 임베딩/청킹 라이브러리 생태계가 Python 중심.
Spring에서 pgvector를 직접 다루는 것보다 한 곳에 AI 처리를 모으는 게 일관성·유지보수에 유리.

### 5.2 왜 큐 기반 비동기인가?

10,000명 규모에서 분석은 글당 수 초 ~ 수십 초 걸림. 동기 HTTP로 받으면 타임아웃·연결 폭주.
큐로 받으면 워커 수만 늘리면 되고, 실패 격리(DLQ)가 자연스러움.

### 5.3 왜 Redis Lua 토큰 버킷인가?

10,000명 중 일부가 폭주하면 OpenAI 한도가 모두 소진됨. 사용자별 한도가 필수.
멀티 API 인스턴스 환경에서 토큰 버킷 갱신은 race condition 위험 → Lua 스크립트로 원자적 처리.

### 5.4 왜 청크 단위 검색인데 문서 단위로 집계하는가?

청크 점수만 정렬하면 "한 문단만 비슷한 글"이 위로 올라옴. 사용자가 보는 단위는 글(문서)이므로
`DISTINCT ON document_id`로 문서당 최고 점수 청크 1개만 추려 정렬.

### 5.5 왜 HNSW + cosine인가?

OpenAI `text-embedding-3-small`(1536d)에서는 cosine similarity가 표준. HNSW는 IVFFlat보다
recall이 안정적이고 sample 데이터가 적은 초기 단계에서도 잘 동작. 추후 데이터가 백만 단위로
커지면 `ef_search` 튜닝 또는 IVFFlat 전환 검토.

### 5.6 왜 OTEL은 optional인가?

운영에선 필수지만, 로컬 개발/단위 테스트에서는 부담. `OTEL_EXPORTER_OTLP_ENDPOINT` 환경변수가
없으면 no-op. 패키지 자체가 없어도 import 안전(try/except).

---

## 6. 환경 변수

`.env.example` 참고. 핵심 항목:

| Key | 기본값 | 의미 |
|---|---|---|
| `DATABASE_URL` | - | `postgresql+asyncpg://user:pw@host/db` |
| `REDIS_URL` | - | `redis://host:port/db` |
| `RABBITMQ_URL` | - | `amqp://user:pw@host/` |
| `OPENAI_API_KEY` | - | OpenAI API 키 |
| `LLM_MODEL` | `gpt-4o-mini` | 채팅·분석용 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 1536차원 |
| `LLM_MAX_CONCURRENCY` | 20 | OpenAI 동시 호출 한도 |
| `WORKER_CONCURRENCY` | 10 | 워커 prefetch_count |
| `WORKER_MAX_RETRIES` | 3 | DLQ 보내기 전 재시도 |
| `CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS` | 800 / 100 | 청킹 파라미터 |
| `MAX_CONVERSATION_TOKENS` | 8000 | 세션 누적 토큰 한도 |
| `MAX_TURNS_PER_SESSION` | 30 | 세션 턴 한도 |
| `CHAT_RATE_CAPACITY` / `CHAT_RATE_REFILL_PER_SEC` | 30 / 0.2 | chat 한도 (사용자별) |
| `ANALYSIS_RATE_CAPACITY` / `ANALYSIS_RATE_REFILL_PER_SEC` | 10 / 0.05 | analysis 한도 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (없음) | 설정 시 트레이싱 활성화 |

---

## 7. 실행 가이드

### 7.1 로컬 (Docker Compose)

```bash
cp .env.example .env
# OPENAI_API_KEY 입력

docker compose up -d --build
docker compose logs -f api worker
```

| URL | 용도 |
|---|---|
| http://localhost:8000 | API |
| http://localhost:8000/docs | Swagger UI |
| http://localhost:8000/health/ready | Readiness probe |
| http://localhost:15672 | RabbitMQ Management (guest/guest) |

### 7.2 마이그레이션

```bash
# 컨테이너 안에서
docker compose exec api alembic upgrade head

# 새 리비전 만들기 (모델 변경 후)
docker compose exec api alembic revision --autogenerate -m "add foo column"
```

`migrations/001_init.sql`은 docker-compose 첫 기동 시 부트스트랩용. 이후 스키마 변경은
**alembic으로만** 관리.

### 7.3 단위 테스트

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/unit
```

현재 23개 테스트 (chunker, query_rewriter, rate_limiter, embedding_cache).
service 레이어 통합 테스트는 SQLAlchemy/pgvector 의존 → docker-compose 환경에서 실행.

---

## 8. 운영 체크리스트

### 8.1 배포 전
- [ ] `OPENAI_API_KEY` 운영 키로 교체
- [ ] `DATABASE_URL`, `REDIS_URL`, `RABBITMQ_URL` 운영 엔드포인트
- [ ] `OTEL_EXPORTER_OTLP_ENDPOINT` 설정 (Datadog/Grafana 등)
- [ ] alembic upgrade head 적용
- [ ] DLQ 모니터링 알람 (`blog.analysis.dlq` depth > 0)
- [ ] OpenAI 사용량 알람 (월 $X 초과 시)

### 8.2 수평 확장
- API: stateless. 세션은 Redis, DB 상태는 Postgres → 인스턴스 추가만 하면 됨
- 워커: `docker compose up --scale worker=N` 또는 k8s replicas

### 8.3 알려진 한계
- **즉시 republish 재시도**: rate limit이 원인이면 즉시 재시도해도 또 실패. delay queue (TTL+DLX) 도입 권장
- **한국어 BM25**: `simple` config라 형태소 분석 약함. mecab 기반 ts config 또는 `pg_bigm` 추가 검토
- **인증 미구현**: 현재 내부 네트워크 신뢰 모델. mTLS 또는 internal API key 도입 필요
- **DLQ 수동 처리**: 운영용 admin 도구 미구현. 현재는 RabbitMQ Management UI에서 처리

---

## 9. 자주 만지는 곳 (Modification Hotspots)

| 작업 | 파일 |
|---|---|
| 청킹 전략 변경 | `app/service/chunker.py` |
| LLM 프롬프트 수정 (분석) | `app/service/analysis_service.py` 상단 `_ANALYSIS_SYSTEM_PROMPT` |
| LLM 프롬프트 수정 (대화) | `app/service/conversation_service.py` 상단 `_CHAT_SYSTEM_PROMPT_TEMPLATE` |
| HyDE 프롬프트 수정 | `app/service/query_rewriter.py` 상단 |
| 검색 쿼리 튜닝 | `app/repository/similarity_repository.py` |
| Rate limit 정책 | `app/core/config.py` (값) / `app/core/rate_limiter.py` (로직) |
| 새 API 엔드포인트 | `app/api/*_router.py` + service 추가 |
| 워커 동작 | `app/worker/analysis_worker.py` |
| DB 스키마 변경 | `app/domain/models.py` 수정 → `alembic revision --autogenerate` |

---

## 10. AI 어시스턴트를 위한 가이드

이 프로젝트에서 코드를 수정할 때 지켜주세요.

### 원칙
1. **레이어 경계 존중**: service에서 HTTP/FastAPI 모르게, repository에서 비즈니스 로직 모르게
2. **외부 의존성은 client/repository로 격리**: service 단위 테스트가 가능한 구조 유지
3. **DTO와 ORM 분리**: 도메인 경계가 흐려지면 안 됨
4. **예외는 도메인 예외로 변환**: `app.core.exceptions`의 클래스 사용, 글로벌 핸들러가 HTTP 응답으로 변환
5. **새 외부 호출에는 타임아웃 + 재시도** 고려: tenacity 패턴 따름
6. **로그는 구조화**: `logger.info("event=foo user_id=%s", user_id)` 형식

### 작업 패턴
- **새 기능 추가 시**: `domain/schemas.py`에 DTO → `service/`에 로직 → `api/`에 라우터 → 단위 테스트
- **DB 변경 시**: `domain/models.py` 수정 → alembic revision 생성 → 검토 후 `alembic upgrade head`
- **외부 API 추가 시**: `client/`에 클라이언트 클래스, retry/concurrency 정책 명시

### 하지 말아야 할 것
- service에서 직접 `redis.Redis()`나 `AsyncOpenAI()` 인스턴스화 (의존성 주입 깨짐)
- repository에서 LLM 호출 (책임 분리 위반)
- 청크/세션 데이터를 메모리에 저장 (multi-instance 환경에서 깨짐)
- `if `로 검색 모드 분기를 service 외부에 노출 (캡슐화 깨짐)

---

## 11. 참고 문서

- [README.md](README.md) — 빠른 시작 가이드
- [docs/CONTRACT.md](docs/CONTRACT.md) — Spring ↔ Python API/MQ 계약
- [.env.example](.env.example) — 환경변수 템플릿
- [requirements.txt](requirements.txt) — 운영 의존성
- [requirements-dev.txt](requirements-dev.txt) — 개발/테스트 의존성

---

## 12. 변경 이력

| 일자 | 내용 |
|---|---|
| 2026-04-25 | 초기 구현: 청킹/유사도/분석/대화 + 큐 워커 + DLQ + Rate limit + 캐시 + Hybrid 검색 + HyDE + 단위 테스트 23개 |
