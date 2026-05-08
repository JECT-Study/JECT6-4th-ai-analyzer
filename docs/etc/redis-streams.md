# Redis Streams 가이드

> 작성일: 2026-05-09
> 관련 문서:
> - [crawl-pipeline.md](crawl-pipeline.md) — 본 프로젝트가 Redis Stream을 어디에서 어떻게 쓰는지
> - [adr-001-redis-streams-for-crawl-pipeline.md](adr-001-redis-streams-for-crawl-pipeline.md) — Redis Stream을 선택한 결정 배경

이 문서는 Redis Stream이 무엇이고 왜 큐로 쓸 수 있는지를 자료구조 관점에서
정리하고, 본 프로젝트가 사용하는 명령어와 1:1로 매핑한다. 이미 Kafka 토픽이나
RabbitMQ work queue에 익숙하다면 §6의 비교 표로 바로 가도 된다.

---

## 1. 한 줄 정의

**Redis Stream은 Redis 5.0에서 추가된 자료구조로, "추가만 가능한 로그
(append-only log)"** 다. Kafka의 토픽과 비슷한 개념을 Redis 안에 가벼운 형태로
구현한 것이라고 보면 된다.

---

## 2. 다른 Redis 자료구조와의 비교

Redis로 큐 비슷한 걸 만드는 방법은 Stream 이전에도 있었다. 주요 선택지는
다음과 같다.

| 자료구조 | 동작 | 한계 |
|---|---|---|
| **List** (`LPUSH` / `BRPOP`) | 양 끝에서 push/pop | 한 번 pop하면 메시지가 사라짐. 여러 consumer가 같은 메시지를 보지 못함. ack/재처리 개념 없음. |
| **Pub/Sub** (`PUBLISH` / `SUBSCRIBE`) | broadcast | 메시지가 영속되지 않음. subscriber가 그 순간 연결돼 있지 않으면 잃어버림. |
| **Stream** (`XADD` / `XREADGROUP`) | append-only 로그 + consumer group | 메시지 영속 + 다중 consumer 분산 + ack/재처리 + 과거 메시지 재읽기 가능 |

직관적으로 정리하면:

- **List**: 한 번 먹으면 사라지는 큐
- **Pub/Sub**: 지금 듣고 있어야만 들리는 라디오
- **Stream**: 녹화된 영상 같은 로그. 누가 어디까지 봤는지 별도 트래킹 가능

---

## 3. Stream의 본질 — append-only 로그

내부 모델은 다음과 같다.

```
Stream "crawl:jobs":
  ┌──────────────────────────────────────────────────────────────┐
  │ ID 1714209600123-0  │ user_id=12, url=https://blog.example/a │
  │ ID 1714209601044-0  │ user_id=12, url=https://blog.example/b │
  │ ID 1714209601044-1  │ user_id=18, url=https://other.com/c    │  ← 같은 ms
  │ ID 1714209604910-0  │ user_id=12, url=https://blog.example/d │
  │ ...                                                           │
  └──────────────────────────────────────────────────────────────┘
                                                  ▲
                                                  └─ 새 메시지는 끝에만 추가
```

특징:

1. **각 entry는 ID + field-value 쌍의 dict** 다. JSON 같은 단일 body가 아니라
   여러 필드를 직접 가진다. 예: `{"url": "...", "user_id": "12", "retry_count": "0"}`.
2. **ID는 자동 생성되고 단조 증가한다.** 기본값은 `<밀리초 timestamp>-<같은 ms 안
   시퀀스>`. 본 프로젝트는 `XADD crawl:jobs * field v ...` 식으로 자동 ID를
   사용한다 (`app/repository/crawl_queue.py:48`).
3. **메시지가 사라지지 않는다.** ack를 해도 entry 자체는 남는다. 메모리 절약을
   원하면 명시적으로 `XTRIM` 또는 `XADD MAXLEN ~ N`으로 잘라야 한다.
4. **AOF/RDB로 디스크에 저장**되므로 Redis가 재시작돼도 살아남는다.

---

## 4. 핵심 개념 — Consumer Group과 PEL

큐로 쓸 때 가장 중요한 개념이다.

```
Stream  crawl:jobs
   │
   ▼
Consumer Group  "crawl-workers"        ← XGROUP CREATE로 생성
   │
   ├──▶ Consumer "worker-host1-pid42"  (이 워커가 메시지 A 받음)
   ├──▶ Consumer "worker-host2-pid17"  (이 워커가 메시지 B 받음)
   └──▶ Consumer "worker-host3-pid88"  (이 워커가 메시지 C 받음)
```

### 4.1 Work queue 의미 보장

같은 Consumer Group 안에서는 **하나의 메시지가 한 명의 consumer에게만 배달된다**.
즉 전형적인 work queue 의미다. 같은 그룹 안 두 워커가 같은 메시지를 동시에 받지
않는다.

다른 Consumer Group을 만들면 **각 그룹이 독립적으로 같은 stream을 소비**한다
(broadcast가 가능). 본 프로젝트는 단일 그룹(`crawl-workers`)만 쓴다.

### 4.2 Pending Entries List (PEL)

각 Consumer Group마다 **PEL** 이라는 별도 자료구조를 유지한다. "누가 어떤
메시지를 받았는데 아직 ack 안 했는지"를 추적하는 장부다.

```
Consumer Group "crawl-workers" PEL:
  ┌────────────────────────────────────────────────────────────┐
  │ ID 1714209600123-0 │ consumer=worker-host1 │ idle=1234ms │ │
  │ ID 1714209601044-0 │ consumer=worker-host2 │ idle=58s    │ │
  └────────────────────────────────────────────────────────────┘
```

- `XACK`을 부르면 PEL에서 제거된다. ack 안 하면 영원히 PEL에 남는다.
- **`XAUTOCLAIM`** 은 PEL을 뒤져서 "idle 시간이 N ms 이상인 메시지"를 다른
  consumer가 가로채올 수 있게 해준다. 이게 **워커가 죽었을 때 stuck 메시지 회수**
  메커니즘이다.

본 프로젝트의 워커 루프(`app/worker/crawl_worker.py:36-44`)가 정확히 이 구조다.

```python
async def run(self) -> None:
    await self._queue.ensure_group()                         # XGROUP CREATE
    while not self._stop_event.is_set():
        claimed = await self._queue.claim_pending(...)       # XAUTOCLAIM ← 죽은 워커 메시지 회수
        messages = claimed or await self._queue.read(...)    # XREADGROUP ← 신규 메시지
        for message in messages:
            await self._handle_message(message)              # 처리 → XACK or 재시도
```

---

## 5. 본 프로젝트의 명령어 매핑

| 코드 위치 | Redis 명령 | 의미 |
|---|---|---|
| `crawl_queue.enqueue` | `XADD crawl:jobs * field1 v1 field2 v2 ...` | 메시지 추가, ID 자동 생성 |
| `crawl_queue.ensure_group` | `XGROUP CREATE crawl:jobs crawl-workers 0 MKSTREAM` | 그룹 최초 1회 생성. 이미 있으면 `BUSYGROUP` 에러 무시 |
| `crawl_queue.read` | `XREADGROUP GROUP crawl-workers <consumer> COUNT 10 BLOCK 5000 STREAMS crawl:jobs >` | `>` = "아직 그룹 누구도 안 받은 새 메시지" |
| `crawl_queue.claim_pending` | `XAUTOCLAIM crawl:jobs crawl-workers <consumer> 60000 0 COUNT 10` | idle ≥ 60s인 PEL 메시지 가로채기 |
| `crawl_queue.ack` | `XACK crawl:jobs crawl-workers <id>` | 처리 완료 인정. PEL에서 제거 |
| `crawl_queue.send_to_dlq` | `XADD crawl:jobs:dlq * ...` | 별도 stream을 DLQ로 사용 |

### 사용되는 환경변수

| Key | 기본값 | 매핑되는 명령 인자 |
|---|---|---|
| `CRAWL_STREAM_NAME` | `crawl:jobs` | stream key |
| `CRAWL_CONSUMER_GROUP` | `crawl-workers` | group name |
| `CRAWL_DLQ_STREAM_NAME` | `crawl:jobs:dlq` | DLQ stream key |
| `CRAWL_BATCH_SIZE` | 10 | `XREADGROUP COUNT` |
| `CRAWL_BLOCK_MS` | 5000 | `XREADGROUP BLOCK` |
| `CRAWL_PENDING_IDLE_MS` | 60000 | `XAUTOCLAIM`의 min-idle-time |
| `CRAWL_MAX_RETRIES` | 3 | DLQ 이동 전 재시도 횟수(앱 레벨) |
| `CRAWL_WORKER_NAME` | (자동 생성) | consumer name. 미설정 시 `worker-{hostname}-{pid}` |

---

## 6. Kafka / RabbitMQ와의 비교

### Kafka 비유

| Kafka | Redis Stream |
|---|---|
| Topic | Stream |
| Partition | (없음 — 단일 stream) |
| Offset | Stream Entry ID (`<ms>-<seq>`) |
| Consumer Group | Consumer Group (이름이 같음) |
| `__consumer_offsets` | Pending Entries List + last-delivered-id |
| commit offset | `XACK` |
| broker 재배치 | `XAUTOCLAIM` |
| log retention | `XTRIM` / `XADD MAXLEN ~` |

다만 Kafka는 **파티션 기반 순서 보장과 대규모 분산**이 강점인 반면, Redis
Stream은 **단일 노드의 메모리 안에서 동작하는 가벼운 스트림**이다. 트래픽이
한 인스턴스가 감당할 수준이면 Redis Stream으로 충분하고, 일정 규모를 넘기면
Kafka로 옮겨야 한다는 의미이기도 하다.

### RabbitMQ work queue 비유

| RabbitMQ | Redis Stream |
|---|---|
| Queue | Stream |
| Consumer | Consumer (in Consumer Group) |
| basic.consume + manual ack | `XREADGROUP` + `XACK` |
| prefetch_count | `XREADGROUP COUNT` |
| Dead Letter Exchange + DLQ | 별도 Stream (`crawl:jobs:dlq`) + 앱이 `XADD`로 적재 |
| TTL + DLX 기반 delay | (native 미지원 — 앱 레벨 zset 등으로 구현 필요) |
| Management UI | (없음 — `XINFO STREAM/GROUP`, `XLEN`, `XPENDING` 등 CLI) |

본 프로젝트가 분석 큐(`blog.analysis`)는 RabbitMQ에, 크롤 큐(`crawl:jobs`)는
Redis Stream에 둔 이유는 [ADR-001](adr-001-redis-streams-for-crawl-pipeline.md)
참조.

---

## 7. 운영용 디버깅 명령

```bash
# stream 길이
XLEN crawl:jobs

# 최근 N개 entry 들여다보기
XREVRANGE crawl:jobs + - COUNT 5

# 그룹 정보 (last-delivered-id, 처리되지 않은 pending 수 등)
XINFO GROUPS crawl:jobs

# 특정 그룹의 PEL 요약 (총 pending 수, 가장 오래된 ID 등)
XPENDING crawl:jobs crawl-workers

# 특정 그룹의 PEL 상세 (consumer별, idle 시간)
XPENDING crawl:jobs crawl-workers - + 10

# 특정 entry의 처리 권리를 강제로 다른 consumer에게 옮기기
XCLAIM crawl:jobs crawl-workers worker-target 60000 <id>

# DLQ 들여다보기
XRANGE crawl:jobs:dlq - +

# stream 메모리 정리 (가장 최근 N개만 남기기, ~는 근사치 OK 의미)
XTRIM crawl:jobs MAXLEN ~ 10000
```

---

## 8. 흔히 빠지는 함정

### 8.1 ack를 안 하면 PEL이 무한히 쌓인다

처리 성공 시 반드시 `XACK`을 해야 한다. 안 하면 PEL에 영원히 남고, `XPENDING`
카운트가 계속 커진다. 본 프로젝트의 `crawl_worker._handle_message`는 성공/실패
모든 분기에서 `xack`을 호출한다.

### 8.2 entry는 ack해도 사라지지 않는다

`XACK`은 *PEL에서만* 제거한다. Stream entry 자체는 그대로 남는다. 메모리/디스크
사용량을 통제하려면 별도 trimming이 필요하다(ADR-001 후속과제).

### 8.3 `XREADGROUP STREAMS ... 0`과 `>`의 차이

- `>`: "그룹에서 아직 누구도 안 받은 신규 메시지". 평소 사용.
- `0` 또는 특정 ID: "내가(이 consumer가) 이미 받았지만 ack 안 한 PEL의 메시지".
  자기 자신의 미처리분 재처리 시 사용.

본 프로젝트는 `>`만 쓰고, 죽은 다른 워커의 PEL은 `XAUTOCLAIM`으로 가져온다.

### 8.4 native delay/지연 재시도 미지원

Redis Stream에는 RabbitMQ TTL+DLX 같은 시간 지연 큐가 없다. 본 프로젝트는
실패 시 즉시 새 entry로 republish하는데, rate limit이 원인인 실패에는 비효율적이다.
"sorted set(score=ready_at_ms) + scheduler" 패턴으로 보강할 수 있다(ADR-001
후속과제).

### 8.5 단일 Redis 인스턴스의 한계

Redis Stream은 단일 노드 자료구조다. 본 프로젝트의 Redis도 single-instance
구성이다. 처리량/내구성 요구가 커지면 Redis Sentinel/Cluster, 또는 Kafka로
이전을 고려해야 한다.

---

## 9. 한 문장 요약

**Redis Stream = "Redis 안에 있는, 영속·재처리·다중 consumer를 지원하는 작은
Kafka"** 정도로 이해하면 된다. 본 프로젝트에서는 크롤 잡 큐로 쓰고 있고,
ack/재시도/DLQ/stuck 회수 4가지 패턴을 §5의 명령어 조합으로 구현해 두었다.
