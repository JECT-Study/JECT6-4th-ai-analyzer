# Spring ↔ Python AI Server Contract

이 문서는 Spring 메인 서버와 Python AI 서버 사이의 통신 계약을 정의합니다.
변경 시 양쪽 팀 모두 합의 후 버전을 올려주세요.

- **현재 버전**: `v1`
- **마지막 갱신**: 2026-05-13
- **호환성 정책**: Backward compatible 변경(필드 추가)은 minor 변경, breaking change는 path prefix를 `/v2/`로 분리

---

## 1. HTTP API

Base URL: `http://analyzer-server:8000`

### 1.1 청킹 + 임베딩 저장

크롤러가 글을 수집한 직후 호출. 분석 서버는 원문을 청킹하고 임베딩을 생성한 뒤
Vector DB(pgvector)에 저장한다. external_id가 있으면 upsert.

유사도 검색은 Spring 메인 서버가 Vector DB를 직접 조회해서 수행한다. 이 API는
검색 API가 아니라 **쓰기 파이프라인(raw text → chunks → embeddings → vector
DB 저장)** 이다.

**Request**
```http
POST /v1/documents/chunks
Content-Type: application/json

{
  "user_id": 12345,
  "source_type": "my_blog",          // "my_blog" | "ext_blog" | "job_posting"
  "title": "Kafka 기반 이벤트 스트리밍 도입기",
  "content": "전체 본문 텍스트 ...",
  "url": "https://blog.example.com/post/123",
  "external_id": "post-123",         // optional, upsert 키
  "metadata": {                      // optional, 자유 형식 JSON
    "tags": ["kafka", "msa"],
    "published_at": "2026-04-20T10:00:00Z"
  }
}
```

**Response 201**
```json
{
  "document_id": 4581,
  "chunk_count": 7
}
```

**Errors**
- `400 VALIDATION_ERROR` — 필수 필드 누락, content 비어있음
- `502 LLM_CLIENT_ERROR` — OpenAI 호출 실패
- `429 RATE_LIMIT_EXCEEDED` — 사용자별 한도 초과 (현재는 적용 안 됨, 추후 적용 예정)

### 1.2 유사도 검색

유사도 검색은 Spring 메인 서버 책임으로 이동한다.

프론트엔드가 유사도 검색을 요청하면 Spring이 사용자 인증/인가, 문서 상태,
source_type 필터, BM25/hybrid 결합, 응답 조립을 담당하고 Vector DB를 직접
조회한다. 분석 서버는 검색 결과를 조회하지 않는다.

**비활성화된 이전 API**

```http
POST /v1/documents/similarity
```

이 API는 v1.3부터 분석 서버에서 제공하지 않는다. 이전 구현은 코드에 주석으로
보존한다.

**책임 경계**
- 분석 서버: raw text 청킹, embedding 생성, Vector DB 저장, 분석/대화
- Spring: Vector DB similarity search, BM25/hybrid 결합, 권한/필터, 결과 응답

**검색어 임베딩 처리**

Spring이 검색어 벡터가 필요하면 다음 중 하나를 선택한다.

1. Spring이 embedding provider를 직접 호출한다.
2. 별도 합의 후 분석 서버에 query embedding 전용 API를 추가한다.

> 이전 계약의 `POST /v1/documents/similarity`는 분석 서버에서 유사도 검색까지
> 수행하는 API였으나, Spring이 웹/API 계층과 Vector DB 조회를 담당하는 구조로
> 정리하면서 비활성화한다.

### 1.3 분석 결과 조회

워커가 처리 완료한 분석 결과를 조회.

**Request**
```http
GET /v1/analysis/documents/{document_id}
```

**Response 200**
```json
{
  "id": 88,
  "document_id": 4581,
  "status": "completed",   // pending | in_progress | completed | failed
  "result": {
    "summary": "...",
    "key_topics": ["Kafka", "MSA"],
    "tone": "분석적",
    "target_audience": "백엔드 개발자",
    "suggestions": ["..."]
  },
  "error_message": null,
  "created_at": "2026-04-25T10:00:00Z",
  "updated_at": "2026-04-25T10:00:30Z"
}
```

### 1.4 대화

분석 결과 기반 대화. 토큰/턴 한도, 사용자별 rate limit 적용.

**Request**
```http
POST /v1/conversations/messages
Content-Type: application/json

{
  "user_id": 12345,
  "session_id": "uuid-v4-string",
  "document_id": 4581,
  "message": "이 글의 톤을 좀 더 캐주얼하게 바꾸려면?"
}
```

**Response 200**
```json
{
  "session_id": "uuid-v4-string",
  "reply": "글의 도입부에서 ...",
  "tokens_used": 1234,
  "tokens_remaining": 6766
}
```

**Errors**
- `400 TOKEN_LIMIT_EXCEEDED` — 세션 누적 토큰 한도 초과 → 새 session_id로 재시작 필요
- `429 RATE_LIMIT_EXCEEDED` — 사용자별 분당 한도. 응답 헤더 `Retry-After`(초) 참고
- `404 NOT_FOUND` — document_id가 user_id 소유가 아님

### 1.5 대화 세션 초기화

```http
DELETE /v1/conversations/{session_id}
```

응답 204.

### 1.6 Health Check

| Path | 용도 | 외부 의존성 점검 |
|---|---|---|
| `GET /health/live` | k8s livenessProbe | X |
| `GET /health/ready` | k8s readinessProbe | DB, Redis, RabbitMQ |
| `GET /health` | live의 alias (호환성) | X |

`/health/ready`는 모든 의존성 OK일 때만 200, 아니면 503.

---

## 2. 에러 응답 표준

모든 에러는 동일 포맷:

```json
{
  "code": "RATE_LIMIT_EXCEEDED",
  "message": "chat rate limit exceeded",
  "retry_after_ms": 4500   // RATE_LIMIT_EXCEEDED 일 때만
}
```

| code | HTTP | 설명 |
|---|---|---|
| `VALIDATION_ERROR` | 400 | 입력 검증 실패 |
| `TOKEN_LIMIT_EXCEEDED` | 400 | 대화 세션 토큰/턴 한도 |
| `NOT_FOUND` | 404 | 리소스 없음 |
| `RATE_LIMIT_EXCEEDED` | 429 | 사용자별 호출 한도. `Retry-After` 헤더 |
| `LLM_CLIENT_ERROR` | 502 | OpenAI 호출 실패 |
| `EXTERNAL_SERVICE_ERROR` | 502 | 기타 외부 의존성 실패 |
| `INTERNAL_ERROR` | 500 | 처리 안 된 예외 |

---

## 3. 메시지 큐 (RabbitMQ)

### 3.1 토폴로지

```
producer (Spring) ──→ blog.analysis (durable, x-dead-letter-exchange=blog.analysis.dlx)
                            │ (consumer 실패)
                            ▼
                       blog.analysis.dlx (fanout)
                            │
                            ▼
                       blog.analysis.dlq (durable)
```

### 3.2 분석 요청 메시지

**Queue**: `blog.analysis`
**Exchange**: default(direct)로 routing_key=`blog.analysis`로 publish

**Body** (JSON)
```json
{
  "user_id": 12345,
  "document_id": 4581
}
```

**Message Properties**
- `content_type: application/json`
- `delivery_mode: 2 (persistent)`

**Headers (선택)**
- `x-app-retry-count`: int — 워커가 자동으로 관리. producer는 보내지 않음.

### 3.3 처리 보장

- **At-least-once**: 워커는 처리 성공 후 `ack`. 실패 시 retry 또는 DLQ.
- **재시도**: 워커가 최대 `WORKER_MAX_RETRIES`(기본 3회)까지 자동 republish.
  중간에 LLM 일시 오류는 워커 내부 tenacity 재시도(3회)에서 흡수.
- **순서 보장 X**: 동일 document_id에 대한 메시지가 동시에 들어오면 마지막 분석 결과만 의미 있음. Spring 측에서 중복 enqueue를 줄이는 것이 좋음.
- **멱등성**: `analyze()`는 호출될 때마다 새 `analysis_jobs` 레코드를 생성. 가장 최근 결과를 사용하면 됨.

### 3.4 DLQ 처리

DLQ(`blog.analysis.dlq`)에 쌓인 메시지는 운영자가 다음 중 하나로 처리:
1. RabbitMQ Management UI에서 확인 후 메시지 폐기
2. 메인 큐로 수동 republish (원인 해결 후)
3. 운영용 admin 도구 (별도 구현 필요)

알림 권장: DLQ depth > 0 일 때 슬랙/PagerDuty 알람.

---

## 4. 인증/인가 (TODO)

현재는 내부 네트워크 신뢰 모델. 운영 배포 시 다음 중 택1:
- **mTLS** (서비스 메시 사용 시)
- **Internal API key** — `X-Internal-Auth: <token>` 헤더로 검증
- **JWT pass-through** — Spring이 사용자 JWT 그대로 전달, Python에서 검증

`user_id`는 본문에 들어있지만 인증 토큰의 sub 클레임과 일치하는지 별도 검증 필요.

---

## 5. 변경 이력

| 버전 | 날짜 | 변경 |
|---|---|---|
| v1.0 | 2026-04-25 | 초기 정의 (chunks, similarity, analysis, conversation, queue) |
| v1.1 | 2026-04-25 | similarity에 use_hyde, use_hybrid, keywords 추가 |
| v1.2 | 2026-04-25 | health check 분리(live/ready), DLQ 토폴로지 명시 |
| v1.3 | 2026-05-13 | 유사도 검색 책임을 Spring으로 이동, 분석 서버는 청킹/임베딩 저장 중심으로 정리 |
