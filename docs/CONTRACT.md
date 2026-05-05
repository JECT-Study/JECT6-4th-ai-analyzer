# Spring ↔ Python AI Server Contract

이 문서는 Spring 메인 서버와 Python AI 서버 사이의 통신 계약을 정의합니다.
변경 시 양쪽 팀 모두 합의 후 버전을 올려주세요.

- **현재 버전**: `v1`
- **마지막 갱신**: 2026-04-25
- **호환성 정책**: Backward compatible 변경(필드 추가)은 minor 변경, breaking change는 path prefix를 `/v2/`로 분리

---

## 1. HTTP API

Base URL: `http://analyzer-server:8000`

### 1.1 청킹 + 임베딩 저장

크롤러가 글을 수집한 직후 호출. external_id가 있으면 upsert.

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

외부 글/공고와 유사한 본인 블로그 글을 검색.

**Request**
```http
POST /v1/documents/similarity
Content-Type: application/json

{
  "user_id": 12345,
  "query_text": "Kafka 기반 백엔드 개발자 채용. Spring, MSA 경험 우대.",
  "target_source_type": "my_blog",
  "top_k": 5,

  // HyDE: 공고/외부 블로그를 가상 본문으로 변환 후 임베딩 (벡터 매칭 정확도↑)
  "query_source_type": "job_posting",
  "use_hyde": true,

  // Hybrid: 벡터 + BM25 결합 (키워드 매칭 정확도↑)
  "use_hybrid": true,
  "keywords": "Kafka Spring MSA"     // optional, 미지정 시 query_text 사용
}
```

**Response 200**
```json
{
  "matches": [
    {
      "document_id": 1023,
      "title": "Kafka 도입 6개월 회고",
      "url": "https://my.blog/post/42",
      "score": 0.8723,
      "matched_chunk_preview": "도입 초기에는 컨슈머 lag이..."
    }
  ],
  "rewritten_query": "나는 Kafka 기반 이벤트 스트리밍 시스템을 ..." // HyDE 사용 시
}
```

**점수 해석**
- `use_hybrid=false`: cosine similarity (-1 ~ 1, 보통 0.7+ 가 의미있음)
- `use_hybrid=true`: RRF score (0~ 작은 값, 절대값보다 순위가 의미)

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
