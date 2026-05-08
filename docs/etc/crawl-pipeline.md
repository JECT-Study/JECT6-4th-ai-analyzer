# 크롤링 파이프라인 아키텍처

> 작성일: 2026-05-09
> 관련 문서:
> - [CONTRACT.md](CONTRACT.md) — Spring ↔ Python 통신 계약
> - [redis-streams.md](redis-streams.md) — Redis Stream 자료구조 자체에 대한 설명
> - [adr-001-redis-streams-for-crawl-pipeline.md](adr-001-redis-streams-for-crawl-pipeline.md) — 큐로 Redis Streams를 선택한 결정 배경

이 문서는 본 서버에 콘텐츠가 들어오는 두 가지 경로와, 그중 크롤링 경로에서
Redis Stream이 어디에 위치하는지를 정리한다. 신규 합류자가 가장 자주 혼동하는
부분이다.

---

## 1. 콘텐츠가 들어오는 두 가지 경로

본 서버에 블로그 본문/공고 본문이 들어오는 길은 **두 가지** 가 있고, 둘은 트리거,
컴포넌트, 동기/비동기성이 모두 다르다.

| 경로 | 진입점 | 누가 본문을 가지고 있나 | Redis Stream | 동기/비동기 |
|---|---|---|---|---|
| **A. 본문 직송** | `POST /v1/documents/chunks` | 호출자(예: Spring 크롤러)가 *이미* 가지고 있음 | 사용 안 함 | 동기 (요청 안에서 청킹/임베딩까지 끝) |
| **B. URL 위임 크롤링** | `POST /v1/crawl/jobs` | URL만 알고 있음. analyzer-server가 직접 fetch | 사용함 (`crawl:jobs`) | 비동기 (`202 Accepted` 후 워커가 처리) |

핵심 차이는 **"누가 외부 사이트로 HTTP fetch를 하느냐"** 이다.

- 경로 A에서는 **호출자(Spring 등)가 외부 사이트를 직접 긁는다**. analyzer-server는
  본문을 받기만 한다.
- 경로 B에서는 **analyzer-server 내부의 `crawl_worker`가 외부 사이트로 직접 HTTP
  fetch한다**. 호출자는 URL만 던진다.

---

## 2. 경로 A — 본문 직송 (`POST /v1/documents/chunks`)

```
Spring 크롤러  ── HTTP fetch ──▶  타깃 사이트 (블로그/공고)
   │
   │ (본문 + 메타데이터를 그대로 가지고)
   ▼
[HTTP] POST /v1/documents/chunks
   │
   ▼
DocumentService.ingest_and_chunk      ◀── 동기 처리 (한 요청 안에서 다 끝남)
   ├── (external_id 있으면 upsert)
   ├── TextChunker.chunk
   ├── EmbeddingCache.get_many → miss는 OpenAI 호출
   └── DocumentRepository.add_chunks (pgvector)

응답 201: {document_id, chunk_count}
```

특징:

- **Redis Stream은 거치지 않는다.** 한 HTTP 요청 안에서 청킹/임베딩까지 끝나고
  응답이 돌아간다.
- 호출자가 이미 본문 추출/정제까지 책임지고 보낸 시나리오용. 즉 "external 시스템이
  자기 크롤러를 굴리고 싶고, 본 서버에는 임베딩/검색만 맡기고 싶다"는 패턴.
- 응답 latency가 OpenAI embedding 호출 시간을 포함한다(=호출자가 그만큼 기다린다).

관련 코드:
- `app/api/document_router.py:14-28`
- `app/service/document_service.py:48-79` (`ingest_and_chunk`)

---

## 3. 경로 B — URL 위임 크롤링 (`POST /v1/crawl/jobs`)

```
호출자 (Spring 등)
   │
   │ (URL만 보냄)
   ▼
[HTTP] POST /v1/crawl/jobs                                ◀──┐
   │                                                          │
   ├── validate_crawl_destination (SSRF 차단)                 │
   ├── retry_after_ms_for_domain  (도메인 1초 간격)           │ analyzer-server
   ├── mark_url_seen              (Set dedup)                 │ 프로세스 경계
   └── XADD crawl:jobs ...                                    │
                                                              │
응답 202: {job_id, stream}                                    │
                                                              │
   ┌─── 비동기 ───────────────────────────────────────┐       │
   │                                                  │       │
   ▼                                                  │       │
Redis Stream  crawl:jobs                              │       │
   │                                                  │       │
   ▼ XAUTOCLAIM(idle≥60s) → XREADGROUP                │       │
crawl_worker                                          │       │
   │                                                  │       │
   ├── wait_for_domain_slot  (도메인별 throttle)      │       │
   ├── CrawlerClient.fetch                            │       │
   │      │                                           │       │
   │      └── [HTTP] ──▶ 타깃 사이트 (블로그/공고)   │       │
   │                                                  │       │
   ├── HtmlExtractor.extract_text/title               │       │
   └── DocumentService.ingest_and_chunk  ──▶ pgvector │       │
                                                      │       │
   처리 결과: XACK (성공) 또는 XADD crawl:jobs:dlq    │       │
   (재시도 횟수가 남았다면 retry_count+1로 재 enqueue)│       │
                                                      └──────┘
```

특징:

- 엔트리 포인트는 **여전히 HTTP API**다. Redis Stream은 호출자와 본 서버 사이가
  아니라 **본 서버 내부의 API 핸들러 ↔ 워커 사이**의 큐다.
- 호출자는 `202 Accepted`를 즉시 받는다. 실제 fetch/추출/청킹/임베딩은 백그라운드
  처리.
- 외부 사이트로의 HTTP fetch는 `crawl_worker` 내부의 `CrawlerClient`가 수행한다.
  별도 외부 크롤러 서비스가 있는 게 아니다.
- 도메인 매너(1초 간격), URL dedup, 재시도, DLQ가 모두 **같은 Redis 인스턴스
  안의 자료구조**로 구현돼 있다.

관련 코드:
- `app/api/crawl_router.py`
- `app/service/crawl_service.py`
- `app/repository/crawl_queue.py`
- `app/worker/crawl_worker.py`
- `app/client/crawler_client.py`
- `app/service/html_extractor.py`
- `app/core/url_security.py` (SSRF 방어)

---

## 4. 자주 혼동하는 포인트 정리

### Q. "Redis Stream을 쓴다"는 건 API 통신을 안 한다는 뜻인가?

아니다. 엔트리 포인트는 여전히 `POST /v1/crawl/jobs` HTTP API다. Redis Stream은
요청을 받아 큐에 넣은 *그 다음* 단계, **본 서버 내부**에서 API 핸들러와 워커를
이어주는 큐다.

### Q. "크롤러 → Redis → analyzer" 식으로 흐르는가?

정확히는 **호출자 → [HTTP] → analyzer API → [Redis Stream] → analyzer worker →
[HTTP] → 타깃 사이트** 흐름이다. "크롤러"와 "analyzer"가 분리된 별도 서비스가 아니라
**같은 프로세스(analyzer-server)의 두 부분**이다.

### Q. 왜 굳이 외부에서 HTTP로 받아놓고 다시 내부 Redis Stream에 넣나?

세 가지 이유로 요약된다.

1. **호출자에게 즉시 응답** (`202 Accepted`) — fetch/임베딩 비용이 사용자
   응답 시간에 포함되지 않게.
2. **dedup·도메인 rate limit·재시도·DLQ를 하나의 인프라로 모으기 위해.**
   Redis Set/Hash/Stream을 같이 쓸 때 atomic 연산이 가능하다.
3. **외부 통신 계약과 내부 파이프라인 분리.** 외부 contract는 RabbitMQ
   `blog.analysis`로만 노출하고, 내부 파이프라인은 내부 자료구조로 처리.

자세한 결정 배경은 [ADR-001](adr-001-redis-streams-for-crawl-pipeline.md)
§5 참조.

### Q. 분석 큐(`blog.analysis`)도 Redis Stream인가?

아니다. **분석 큐는 RabbitMQ**고, **크롤 큐는 Redis Stream** 이다. 둘을 분리한
이유는 다음과 같다.

| 경로 | 사용 | 이유 |
|---|---|---|
| `blog.analysis` | RabbitMQ | Spring 메인 서버가 publish하는 *외부 통신 계약*. AMQP 표준 인터페이스가 안정적인 contract 표면에 더 적합. |
| `crawl:jobs` | Redis Stream | 본 서버 내부 파이프라인. dedup·도메인 rate limit이 같은 Redis 안 자료구조로 자연스럽게 묶임. |

---

## 5. 처리 보장과 재시도

### 경로 B의 메시지 라이프사이클

```
XADD                                XACK
  │                                   ▲
  ▼                                   │
[stream entry] ─▶ XREADGROUP ─▶ [PEL: 처리 중] ─▶ 정상 완료
                                    │
                                    │ 처리 실패
                                    ▼
                       retry_count < CRAWL_MAX_RETRIES?
                                    │
                       ┌────── yes ─┴── no ──────┐
                       ▼                          ▼
              새 entry로 XADD            XADD crawl:jobs:dlq
              (retry_count += 1)         (영구 실패 격리)
              이전 entry는 XACK          이전 entry는 XACK
```

### Stuck 메시지 회수

워커가 메시지를 받은 뒤(=PEL에 등록된 뒤) ack 전에 죽으면, 그 메시지는 PEL에
영원히 남을 수 있다. 이를 막기 위해 워커 루프는 매 사이클마다 **먼저
`XAUTOCLAIM`을 호출**한다.

```python
# app/worker/crawl_worker.py:36-44
async def run(self) -> None:
    await self._queue.ensure_group()
    while not self._stop_event.is_set():
        claimed = await self._queue.claim_pending(self._consumer_name)  # XAUTOCLAIM
        messages = claimed or await self._queue.read(self._consumer_name)  # XREADGROUP
        for message in messages:
            await self._handle_message(message)
```

`XAUTOCLAIM`은 PEL에서 idle 시간이 `CRAWL_PENDING_IDLE_MS`(기본 60초) 이상인
메시지를 자동으로 현재 consumer 앞으로 가져온다. 즉 죽은 워커의 메시지가
60초 이상 멈춰 있으면 살아 있는 다른 워커가 인계해서 처리한다.

### 멱등성 주의

경로 B에서 retry 시 메시지가 새 entry로 다시 등록된다. 따라서 같은 URL이
여러 번 처리될 가능성이 있다. 이를 다음 두 곳에서 흡수한다.

1. **URL Set dedup** (`crawl:seen:urls`) — enqueue 단계에서 중복 차단.
2. **Document upsert** (`external_id` 일치 시 기존 청크 삭제 후 재생성) —
   ingest 단계에서 중복 처리.

---

## 6. 운영 관점 체크리스트

- **Stream trimming**: `crawl:jobs` / `crawl:jobs:dlq` 모두 ack 후에도 entry가
  남는다. 주기 `XTRIM` 또는 `XADD MAXLEN ~ N` 정책이 필요. (ADR-001 후속과제)
- **DLQ 모니터링**: `XLEN crawl:jobs:dlq > 0` 알람 권장.
- **Pending 누적 모니터링**: `XPENDING crawl:jobs crawl-workers` 결과의 미처리
  카운트가 비정상적으로 늘면 워커가 stuck이거나 부족.
- **수평 확장**: consumer name이 `worker-{hostname}-{pid}`로 자동 유니크하므로
  k8s replicas로 그냥 늘리면 된다.
- **도메인 매너**: `CRAWL_DOMAIN_DELAY_SECONDS`로 강제. 1초 미만으로 줄이면
  타깃 사이트 차단 위험.

---

## 7. 정리

| 경로 | 트리거 | analyzer-server가 외부 사이트로 HTTP를 거는가 | Redis Stream | 응답 시점 |
|---|---|---|---|---|
| A | `POST /v1/documents/chunks` | ❌ | ❌ | 청킹/임베딩 끝난 뒤 (동기) |
| B | `POST /v1/crawl/jobs` | ✅ (`crawl_worker`) | ✅ (`crawl:jobs`) | 즉시 `202` (비동기) |

Redis Stream은 **B 경로에서 분, analyzer-server 프로세스 안의** API ↔ worker
큐다.
