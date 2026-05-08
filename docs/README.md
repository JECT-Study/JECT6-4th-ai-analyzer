# docs/

본 서버의 인터페이스 계약과 아키텍처 설명서를 모은 곳.

| 문서 | 무엇을 다루는가 | 누가 읽으면 좋은가 |
|---|---|---|
| [CONTRACT.md](CONTRACT.md) | Spring 메인 서버 ↔ 본 서버의 HTTP API + RabbitMQ 메시지 계약 | 양 팀 모두. 변경 시 양쪽 합의 필요. |
| [crawl-pipeline.md](crawl-pipeline.md) | 콘텐츠가 들어오는 두 가지 경로(`/v1/documents/chunks` vs `/v1/crawl/jobs`)와 Redis Stream의 정확한 위치 | 처음 합류한 개발자, 크롤 파이프라인을 손볼 사람 |
| [redis-streams.md](redis-streams.md) | Redis Stream 자료구조 자체에 대한 설명 + 본 프로젝트 명령어 매핑 | Redis Stream 또는 Kafka/RabbitMQ를 처음 접하는 사람, `crawl_queue` 코드를 읽어야 하는 사람 |
| [adr-001-redis-streams-for-crawl-pipeline.md](adr-001-redis-streams-for-crawl-pipeline.md) | 크롤 큐로 Redis Streams를 채택한 결정 배경(Context/Options/Decision/Consequences) | "왜 분석은 RabbitMQ인데 크롤은 Redis야?"라는 질문이 있는 사람 |

내부 작업 노트(완성된 문서가 아니라 그때그때의 분석/계획)는 `../ai_logs/` 아래에
있다.
