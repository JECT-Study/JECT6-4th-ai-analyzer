# ADR-001: 크롤링 파이프라인의 메시지 큐로 Redis Streams 채택

- **상태(Status)**: Accepted
- **작성일**: 2026-05-09
- **결정자**: 최현호
- **관련 코드**:
  - `app/repository/crawl_queue.py`
  - `app/worker/crawl_worker.py`
  - `app/service/crawl_service.py`
  - `app/api/crawl_router.py`
- **관련 커밋**: `2bdb9a1 add : Redis Stream 기능 추가`

---

## 1. 용어 정리

이 문서에서 말하는 "크롤링 파이프라인"은 다음 흐름을 가리킨다.

```
POST /v1/crawl/jobs  ──▶  CrawlService.enqueue
                                │
                                ▼  (큐: 이 ADR의 결정 대상)
                          crawl:jobs (Redis Stream)
                                │
                                ▼
                          CrawlWorker
                                ├─▶ HTTP fetch (CrawlerClient)
                                ├─▶ HtmlExtractor (본문 추출)
                                └─▶ DocumentService.ingest_and_chunk
                                        ├─▶ TextChunker (in-process, 동기)
                                        ├─▶ EmbeddingCache (Redis)
                                        └─▶ pgvector 저장
```

즉 **청킹 자체는 워커 프로세스 안에서 동기 실행**되고, 이 ADR이 선택한 것은
청킹을 트리거하는 **상위 작업 큐(crawl job queue)**다. 이후 본문에서 "큐"라고
부르는 것은 이 부분을 의미한다.

---

## 2. 컨텍스트(Context)

### 2.1 이미 존재하는 비동기 채널

본 서버에는 RabbitMQ 기반 큐 하나가 이미 존재한다.

- **`blog.analysis` 큐** — Spring 메인 서버가 publish하고, `AnalysisWorker`가
  consume한다.
- DLX/DLQ 토폴로지(`blog.analysis.dlx` → `blog.analysis.dlq`), `x-app-retry-count`
  헤더 기반 재시도 정책이 정착돼 있다.
- RabbitMQ는 `docker-compose.yml`/배포 환경에서 **외부(Spring)와의 비동기 통신
  계약 표면**이다(`docs/CONTRACT.md` §3 참고).

### 2.2 새로 필요해진 요구사항

블로그 본문 자동 수집 기능이 추가되면서, 다음과 같은 *내부 전용* 비동기
파이프라인이 필요해졌다.

1. `POST /v1/crawl/jobs`로 받은 URL을 즉시 응답(`202 Accepted`) 후 백그라운드
   처리.
2. 동일 URL 중복 큐잉 방지(dedup).
3. **도메인 단위 rate limit** — 같은 도메인을 1초 간격 이상으로 호출(타깃 사이트
   매너 + 차단 회피).
4. SSRF 방어(localhost/private/metadata 차단) — 정책은 별도 모듈
   (`app/core/url_security.py`)로 이미 분리됨.
5. consumer 다운/지연 시 **stuck 메시지 회수**가 가능해야 함.
6. 영구 실패는 별도 격리(DLQ)되어야 함.

### 2.3 환경 가정

- Redis는 이미 *세션 / 임베딩 캐시 / Lua 토큰 버킷 rate limit* 용도로 운영
  중이다(자세한 내용은 `redis_integration_codex.md` 참고).
- RabbitMQ는 외부 contract 용도로만 운영 중이며, 내부 전용 토픽/큐는 아직 없다.
- 운영 인력은 Redis와 RabbitMQ 둘 다 관리하고 있다.
- 크롤링은 본 서버 내부에서 시작되어 본 서버 내부에서 끝난다(외부 시스템이
  publish하지 않는다).

---

## 3. 검토한 대안(Options Considered)

### Option A — RabbitMQ에 큐 하나 더 추가

- 이미 운영 중인 인프라 재사용.
- DLX/DLQ, persistent delivery 등 패턴이 익숙.
- 하지만 다음 보조 자료구조까지 RabbitMQ에 자연스럽게 넣기 어렵다:
  - 도메인 마지막 호출 시각(`crawl:ratelimit:domain` Hash)
  - URL dedup Set(`crawl:seen:urls`)
- 이런 부속 데이터는 결국 Redis로 가게 된다 → **이중 관리**가 시작됨.

### Option B — Redis Streams (XADD / XREADGROUP / XAUTOCLAIM)

- consumer group 모델이 RabbitMQ의 work queue와 비슷하면서, Kafka처럼 메시지가
  스트림에 영속된다.
- `XAUTOCLAIM`으로 idle한 pending 메시지를 다른 consumer가 인계받을 수 있어
  컨테이너 다운/배포 시 안전하다.
- 큐 동작과 dedup·도메인 rate limit이 **같은 Redis 안에서** 처리되어 자료
  구조 일관성이 좋다.
- DLQ도 또 다른 Stream(`crawl:jobs:dlq`)으로 단순 표현 가능.

### Option C — Postgres 기반 큐 (`SELECT ... FOR UPDATE SKIP LOCKED`)

- 새 인프라 추가 없음.
- 트랜잭션과 함께 묶기 좋음.
- 하지만:
  - 도메인 rate limit·dedup은 결국 Redis 또는 DB row 락 위에 만들어야 함.
  - polling 모델이라 latency 제어가 거칠다.
  - 본 서버 DB는 pgvector 워크로드(검색)가 핵심이라 큐 polling 부하가 같은
    인스턴스에 얹히는 것이 부담스럽다.

### Option D — Kafka

- 영속 로그/리플레이/스케일에 강함.
- 그러나 인프라 도입 비용이 크고, 현재 트래픽(크롤링 잡 수십~수백/min 추정)에
  비해 과대 적합. 운영 인력 학습 비용도 크다.

---

## 4. 결정(Decision)

**크롤링 파이프라인의 작업 큐로 Redis Streams를 채택한다.**

구체적 토폴로지는 다음과 같다.

| 자료구조 | 키 | 용도 |
|---|---|---|
| Stream | `crawl:jobs` | 메인 작업 큐 |
| Consumer Group | `crawl-workers` | 다중 워커 분산 처리 |
| Stream | `crawl:jobs:dlq` | 영구 실패 메시지 격리 |
| Set | `crawl:seen:urls` | URL 중복 큐잉 차단 |
| Hash | `crawl:ratelimit:domain` | 도메인별 마지막 호출 시각 |

운영 파라미터는 `app/core/config.py`에 환경변수로 노출:

- `CRAWL_STREAM_NAME=crawl:jobs`
- `CRAWL_CONSUMER_GROUP=crawl-workers`
- `CRAWL_DLQ_STREAM_NAME=crawl:jobs:dlq`
- `CRAWL_BATCH_SIZE=10`
- `CRAWL_BLOCK_MS=5000` (XREADGROUP block 시간)
- `CRAWL_PENDING_IDLE_MS=60000` (XAUTOCLAIM idle 임계값)
- `CRAWL_MAX_RETRIES=3` (DLQ 이동 전 재시도)
- `CRAWL_DOMAIN_DELAY_SECONDS=1` (도메인 매너)

### 동시에, RabbitMQ는 "외부 contract" 용도로만 유지한다

`blog.analysis` 큐는 그대로 RabbitMQ에 둔다. 이유는:

- Spring 메인 서버가 producer이므로 RabbitMQ의 표준 AMQP 인터페이스가
  *공식 통신 계약*에 더 적합하다.
- DLX/메시지 우선순위/관리 UI 등 RabbitMQ 생태계가 이 시나리오에 잘 맞는다.
- 즉, 이 ADR은 "전부 한 큐로 통일"이 아니라 **"외부 계약은 RabbitMQ, 내부
  파이프라인은 Redis Streams"** 라는 책임 분리를 명시한다.

---

## 5. 결정 근거(Rationale)

### 5.1 자료구조 응집(co-location)이 큰 이점

크롤 파이프라인은 큐 동작과 함께 다음 보조 연산이 매 메시지마다 필요하다.

```
enqueue 직전:
  - SADD  crawl:seen:urls   url        (중복 차단)
  - HGET  crawl:ratelimit:domain  host (도메인 최근 호출 시각)
  - HSET  crawl:ratelimit:domain  host now

워커 처리 중:
  - 같은 키들을 다시 읽고 갱신
```

이 연산이 **큐와 같은 Redis 인스턴스 안에 모여 있을 때** atomic 연산
(`SADD`/`HSET`/Lua)으로 일관성 있게 다룰 수 있다. 만약 큐만 RabbitMQ로 가면
RabbitMQ ↔ Redis 사이에 트랜잭션 경계가 두 개 생겨, 양쪽 정합성을 맞추는 보상
로직이 늘어난다.

`CrawlService.enqueue`(line 21-35)의 보상 패턴이 그 예다.

```python
if not await self._queue.mark_url_seen(url):
    raise ValidationError("crawl url already queued or processed")
try:
    job_id = await self._queue.enqueue(...)
except Exception:
    await self._queue.unmark_url_seen(url)   # ← 같은 Redis라 단순함
    raise
```

같은 Redis 안에서 일어나니 실패 보상이 짧다.

### 5.2 책임 분리 — 외부 계약과 내부 파이프라인을 같은 인프라에 섞지 않는다

`blog.analysis`(RabbitMQ)는 Spring과의 **공식 contract 표면**이다. 외부 시스템
관점에서 RabbitMQ 인터페이스는 안정적이어야 한다.

반면 크롤링은 **본 서버 내부 구현**이다. 외부 producer가 없고, 메시지 포맷이나
리트라이 정책 변경이 본 서버 내부에서 자유롭다. 두 경로를 같은 인프라에 두면
"외부 영향이 두려워서 내부 정책도 못 바꾸는" 결합이 생기기 쉽다. 분리하는 편이
변경의 자유도가 더 크다.

### 5.3 Redis Streams는 본 서비스 요구사항을 충분히 만족한다

| 요구사항 | Redis Streams 대응 |
|---|---|
| 다중 consumer 분산 | Consumer Group(`XREADGROUP`) |
| at-least-once | `XACK` 명시 ack |
| stuck 메시지 회수 | `XAUTOCLAIM` (idle 임계값으로 자동 인계) |
| 영구 실패 격리 | 별도 Stream `crawl:jobs:dlq`에 `XADD` |
| 메시지 영속 | AOF + Stream은 디스크 기록 |
| 헤더/메타 | Stream entry의 field-value (예: `retry_count`) |
| consumer 식별 | consumer name = `worker-{hostname}-{pid}` 자동 생성 |

코드에서는 `app/repository/crawl_queue.py`가 위 API를 thin wrapper로 노출하고,
`app/worker/crawl_worker.py`가 다음 루프로 처리한다.

```
1) xautoclaim(idle ≥ 60s) — 죽은 워커의 pending 회수
2) xreadgroup(">", count=N, block=5s) — 신규 메시지
3) 각 메시지 처리 → xack 또는 retry/DLQ
```

이 패턴은 RabbitMQ의 prefetch + manual ack와 의미적으로 동등하면서 인프라가
하나 줄어든다.

### 5.4 Redis는 이미 critical-path에 있다

대화 세션, 임베딩 캐시, rate limit이 이미 Redis에 의존하므로, **Redis가 죽으면
서비스는 어차피 의미 있게 동작하지 못한다.** 따라서 "Redis Streams를 쓰면 SPOF
의존성이 늘어난다"라는 우려는 실질적으로 비용이 거의 0이다(이미 같은 SPOF
선상). 새 인프라(Kafka 등)를 도입할 때의 *추가* 운영 부담이 더 크다.

### 5.5 운영 단순화

같은 인프라(Redis)를 다양한 용도로 일관되게 쓰면:

- 백업/복구 절차 통일.
- 모니터링 도구가 같다(Redis INFO, slowlog, latency monitor).
- AOF/메모리 정책을 한 번만 결정하면 된다(현재 `--maxmemory 1gb
  --maxmemory-policy noeviction`).
- 인프라 추가 추가 없이 시작할 수 있어 초기 도입 비용이 낮다.

---

## 6. 결과(Consequences)

### 6.1 좋아진 점 (Positive)

- 크롤 파이프라인의 모든 상태(큐/dedup/도메인 rate limit/DLQ)가 한 인프라 안에
  모여 코드가 단순하다.
- 외부 계약(RabbitMQ `blog.analysis`)과 내부 파이프라인의 변경 영향이 분리된다.
- 새 인프라 도입 없이 즉시 출시 가능. 추후 트래픽이 커지면 분리 가능한 구조다.
- 워커가 죽어도 `XAUTOCLAIM` 덕분에 메시지 손실 없이 다른 워커가 인계받는다.

### 6.2 받아들여야 하는 단점 (Negative / Trade-offs)

- **delay/지수 백오프 재시도가 native가 아님.** 현재 `CrawlWorker._handle_failure`
  는 즉시 재 enqueue(`XADD`)한다. rate limit이 원인인 실패에는 비효율적이다.
  - 완화 방법: future work로 "지연 재시도 zset(score=ready_at_ms)" 도입을
    검토(§7).
- **메모리/디스크 사용량 관리 필요.** Stream은 `MAXLEN ~`/`XTRIM`으로 트림하지
  않으면 무한 누적된다. 현재는 ack 후에도 entry가 남는 구조라 운영 시 주기적
  trimming 정책이 필요하다.
- **운영 도구가 RabbitMQ Management UI보다 미숙.** 메시지 단위 조회·재시도는
  CLI(`XRANGE`, `XINFO STREAM/GROUP`)로 해야 한다. 운영용 admin 도구는 별도
  과제.
- **transactional outbox 패턴이 한쪽으로 쏠림.** 이후 "DB 변경과 큐 enqueue를
  원자적으로 묶고 싶다" 같은 요구가 생기면, RabbitMQ보다는 Redis 쪽으로 패턴이
  쏠리게 된다(이미 그렇게 가고 있음).
- **Redis SPOF 가중.** 5.4에서 "이미 SPOF다"라고 했지만, 의존하는 critical-path
  기능이 늘어났다는 사실은 백업·HA(Sentinel/Cluster) 구성을 더 진지하게 만든다.

### 6.3 중립 (Neutral)

- 두 큐 시스템(RabbitMQ + Redis Streams)을 동시에 운영해야 한다. 본 서버에서는
  목적이 다르므로 의도적 결정이지만, 신규 합류자에게는 "왜 둘 다?"라는 학습
  포인트가 한 번 더 생긴다 — 본 ADR이 그 답이다.

---

## 7. 후속 과제 (Follow-up)

> 이 ADR과 함께 트래킹할 후속 작업 목록.

1. **Stream trimming 정책 결정** — `XADD MAXLEN ~ N` 또는 주기 `XTRIM`.
   `crawl:jobs`/`crawl:jobs:dlq` 모두 대상.
2. **지연 재시도 메커니즘** — 현재 즉시 republish는 rate limit/일시 장애에서
   비효율. zset(`ready_at` score) + scheduler 또는 단순 sleep 기반 backoff
   고려.
3. **DLQ 운영 도구** — `crawl:jobs:dlq`/`blog.analysis.dlq` 둘 다 수동 처리
   상태. 작은 admin endpoint(인증 분리)로 enumerate/replay 제공 검토.
4. **Redis HA 검토** — Sentinel 또는 Cluster. 현 구성은 single-instance.
5. **메트릭 노출** — `XLEN`, `XPENDING` 결과를 OpenTelemetry/Prometheus로 노출
   (큐 적체·pending 누적 알람).

---

## 8. 참고

- `app/repository/crawl_queue.py` — Redis Streams API thin wrapper
- `app/worker/crawl_worker.py` — `xautoclaim` + `xreadgroup` 루프
- `app/service/crawl_service.py` — enqueue 시 dedup/도메인 rate limit 적용
- `app/core/url_security.py` — SSRF 방어 (큐 결정과는 직교)
- `CONTRACT.md` §3 — RabbitMQ `blog.analysis` 외부 계약
- `crawl-pipeline.md` — 본 결정이 적용된 크롤 파이프라인의 구체적인 흐름
- `redis-streams.md` — Redis Streams 자료구조 자체에 대한 설명/명령어 매핑
- `../ai_logs/redis_integration_codex.md` — Redis 사용처 전반
- `../ai_logs/project.md` §5.1 — "왜 두 개의 큐 시스템인가?" 요약
