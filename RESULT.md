# cweb 안전 재시작 명령
result: partial

주인님, `atrium cweb restart/status/cancel/recover`를 격리 브랜치에 구현했어요. SQLite 트랜잭션으로 새 작업 획득을 멈추고, 실행 중인 작업이 끝난 뒤 독립 tmux 감시자가 재시작해요.

- 브랜치: `feat/cweb-safe-restart`, 경로: `/data/jobs/codex-jobs/cweb-safe-restart`
- 격리 단위 검사 8개 통과: 실행 중 작업과 대기 작업, 취소, 시작 실패, 타임아웃, 기존 큐 우선순위·동시 초기화.
- 전체 unittest는 body 관련 실패와 환경 의존 오류가 나타나 중단했어요. 전체 통과는 확인하지 못했어요.
- 재시작 동작의 subprocess는 모의 처리했어요. 실제 cweb 재시작과 HTTP 확인은 하지 않았어요.
- 기존에 돌아가는 worker는 새 획득 차단을 모릅니다. 배포 시 먼저 모든 worker를 안전하게 새 버전으로 교체해야 해요.
- `done`은 tmux 세션이 생긴 것까지만 확인하며 HTTP 건강 상태는 별도 확인이 필요해요. 감시자가 죽으면 `status` 확인 후 30초 뒤 `recover`로 잠금을 복구할 수 있어요.
- 커밋, 운영 배포와 재시작은 하지 않았어요. 코라 선배 검토가 필요해요.
