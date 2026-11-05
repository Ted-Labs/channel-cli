# channel-cli 발전 로드맵 v2

- **버전:** 2.0  
- **날짜:** 2026-10-30  
- **이전:** `channel-cli-roadmap.md` (v1)  
- **대상:** [Ted-Labs/channel-cli](https://github.com/Ted-Labs/channel-cli)  
- **현재 패키지 버전:** 0.1.0  
- **관련 문서:**  
  - `channel-cli-architecture-v2.md`  
  - `adr-intake-router-integration.md`

---

## 0. v1 → v2 방향 전환

v1 로드맵은 “에이전트별 워커 병렬”을 P0에 가깝게 두었다.  
논의 결과, 아래처럼 중심을 옮긴다.

| v1 강조 | v2 강조 |
|---------|---------|
| 인프라 워커 병렬·처리량 | **본체 주도 분해·위임·병합** |
| Intake / Router / Agent queue 분리 전제 | **단일 큐 + enqueue 시 라우팅 (통합)** |
| 병렬 = 여러 워커 | 병렬 = 본체가 분신(subagent)에 위임 |
| 일반적 확장성 | **Tailscale 개인망 원격 서버 (단일 운영자)** 우선, 분산은 트리거 후 |

분신 모델 수준: **본체 = 프론티어**, **분신 = 1~2단계 아래**.  
분신 실행 경로: **본체가 지원하는 경우 in-session subagent를 우선**한다. 지원 계약을 확인하기 전까지 본체별 구현으로 단정하지 않는다.

---

## 1. 현황 요약

channel-cli는 채널 메시지를 **서버상 durable 큐**에 넣고, 워커가 AI CLI를 실행한 뒤 events로 결과를 돌리는 Atrium 실행 구성요소다.  
운영 전제: **Tailscale 개인망 원격 서버 (단일 운영자)**.

### 강점
- 외부 런타임 의존성 없음 (Python 3.11+, SQLite)
- `(channel, room, agent)` 세션 분리
- `message-id` 중복 방지
- body 모드 (`codex-body` 등)
- 채널 JSON 계약이 명확

### 작업 브랜치 상태와 남은 차이 (2026-11-01)

| 영역 | 현재 상태 | 다음 차이 |
|------|-----------|-----------|
| 큐·워커 | 단일 SQLite priority 큐, worker 1개; priority 내림차순, 동점 FIFO | session 단위 점유 제한 없음 |
| 라우팅 | enqueue-time `RoutingDecision(agent, prompt, priority)`; 기본 priority 90, `--priority`로 지정 가능 | 요청 종류별 자동 priority/tier 정책 없음 |
| Body 제어 | Codex·Cora·Gemini의 기본 interrupt 운영 반영 | Cora/Gemini steer 및 우선 대화 전달 정책 미완료 |
| job 구조 | 기본 lifecycle, retry, attempt별 중단 요청, priority와 기존 DB migration | tier/parent/root/spawn_mode/child 병합 없음 |
| 관측성 | `jobs`, `events`, `result` 및 실행 로그 | 요약 status/tree와 사용량 추적 없음 |

이 표는 현재 작업 브랜치 기준이다. 운영 반영을 뜻하지 않으며, 목표 구조를 현재 구현으로 서술하지 않는다. 상세 상태는 `channel-cli-architecture-v2.md`의 코드 상태 표를 따른다.

---

## 2. 목표 아키텍처 (로드맵이 향하는 곳)

```text
send → route(agent, priority, tier) → 단일 priority 큐
     → worker (agent/session 동시성)
     → adapter → 본체
                    └── in-session 분신 (기본)
```

- 대화·지시 동일 경로, priority로 구분  
- Tailscale 개인망 원격 · 단일 운영자 가정에서 Intake/Router **통합** 유지  
- 분리 재검토는 ADR에 정의된 트리거가 있을 때만  

상세: `channel-cli-architecture-v2.md`

---

## 3. 우선순위 (v2)

### P0 — 단일 큐의 라우팅·스케줄 기반

#### 3.1 Job 메타 및 SQLite 동시성 설정 확인
- 먼저 필요한 필드만 확정한다: `priority`, `tier`, 추후 parent/root 식별자. child 기능 전 `spawn_mode`와 child 상태를 미리 강제하지 않는다.
- `queued`, `running`, `done`, `failed`, `interrupted`를 기본 상태로 유지한다. `waiting_children`·`dead_letter`는 해당 기능을 구현할 때 추가한다.
- WAL을 사용한다. `sqlite3.connect(timeout=15)`와 `PRAGMA busy_timeout=15000`으로 15초 busy handler를 유지한다. 기존 설정을 보존한 결정이며, 운영 잠금 대기는 별도 관측한다.

**왜 P0인가**  
이후 라우팅·분신·병합·관측 및 동시 접근 안정성이 모두 이 기반에 의존한다.

#### 3.2 enqueue 시 라우팅 (통합 경로)
- 기존 멘션·기본 에이전트 규칙을 순수한 `route()`로 정리한다.
- 초기 RoutingDecision은 기존 정리된 prompt를 보존하며 agent, prompt, priority를 포함한다. tier는 실제 정책이 생길 때 추가한다.
- 외부 모델/API 없이 결정적으로 라우팅하며, 단일 큐/worker를 유지한다.

**근거:** `adr-intake-router-integration.md`

#### 3.3 단일 큐 priority 스케줄
- 대화와 작업은 같은 큐에 둔다. 사용자가 기다리는 요청을 우선할 기본값을 정의한다.
- 같은 priority끼리는 FIFO를 유지한다.
- 사용자 대면 요청은 기본 priority 90이며 `send --priority 0..100`으로 명시할 수 있다.
- 첫 구현에서는 worker 1개를 유지한다. 실행 중 turn을 선점하는 동작은 priority가 아니라 별도 steer/interrupt 계약이다. 기본 interrupt는 이미 운영 반영되었고, 남은 steer는 본체별 capability에 맞춘다.

---

### P1 — 세션·본체 조율과 운영 루프

#### 3.4 세션 점유와 동시성
- 같은 `(agent, session)` 작업은 직렬을 기본으로 한다.
- 동시 실행은 큐 기록으로 병목을 확인하고 권한 공간이 분리된 경우만 늘린다.

#### 3.5 본체별 분신 capability
- Claude·Codex·Gemini의 subagent, steer, interrupt, 세션 재개 지원을 표로 확인한다.
- 지원 경로부터 in-session subagent를 적용한다. 미지원 본체의 독립 세션 분신은 자동 생성하지 않는다.

#### 3.6 waiting_children + 병합 재진입 (Wakeup)
- (예외) child job 사용 시 parent가 `waiting_children`으로 대기
- 마지막 child job 종료 시 Worker가 parent job을 `queued`(우선순위 95)로 자동 Wakeup
- 본체의 병합·테스트 후 root `done` + event
- 채널에는 root 결과 중심 노출

#### 3.7 재시도 + dead letter
- max_attempts, backoff
- 재시도 가능 오류 vs 영구 실패
- DLQ / `channel retry` 운영 명령

#### 3.8 Body 모드 기능 맞추기
- 남은 steer 및 고우선순위 대화 전달을 본체별 capability에 맞춰 정렬한다.
- 기본 interrupt는 구현·운영 반영 완료로 다시 계획에 넣지 않는다.

#### 3.9 기본 관측성
- `channel status`, `channel jobs --tree`, 큐·running 요약
- job별 소요 시간·시도 횟수

---

### P2 — 중기 강화

| 항목 | 내용 |
|------|------|
| 로그·데이터 GC | retention, `channel gc` |
| 어댑터 플러그인화 | 새 CLI를 설정/플러그인으로 추가 |
| tier·사용량 추적 | 본체/분신 비용·품질 확인 |
| 선택적 세션 공유 | 기본 off, 방 단위 명시 옵션만 |
| 우선순위 정책 고도화 | Jev 가중치·난이도·잔여 부하 |

---

### P3 — 장기 / 조건부

| 항목 | 전제 |
|------|------|
| Intake / Router 런타임 분리 | ADR 재검토 트리거 충족 시 |
| 분산 워커 | 단일 서버·개인망을 넘어설 때 |
| 샌드박스·권한 계층 | room/agent별 격리 강화 |
| 결과 웹훅 푸시 | 폴링만으로 부족할 때 |

---

## 4. 다음 액션 순서

1. **기준선과 계약 고정:** job lifecycle, `send`/events/자동 게시 경계, priority 정렬과 동점 FIFO 동작을 문서와 contract test로 고정한다.
2. **Job 메타·SQLite 설정:** 필요한 필드만 추가하고 WAL/busy timeout 및 구형 DB migration을 검증한다.
3. **순수 라우팅 함수:** 기존 멘션·기본 에이전트 선택을 `route(input) -> RoutingDecision`으로 추출한다. 외부 모델 호출 없이 결과가 결정적이어야 한다.
4. **Priority 스케줄:** 우선순위와 FIFO tie-break를 단일 큐에 적용한다. 첫 버전은 worker 1개를 유지한다.
5. **세션 점유·동시성:** 실제 작업 기록으로 병목을 확인한 뒤 agent/session별 limit을 추가한다.
6. **Capability 기반 본체 위임:** 세 본체 기능을 확인한 다음 지원 경로부터 subagent와 steer를 구현한다.
7. **Parent/child·병합:** 독립 child session이 필요할 때만 추가한다.
8. **관측·복구:** status/tree, retry backoff, dead-letter는 운영 요구에 맞춰 단계별로 도입한다.

각 단계는 독립적으로 검증하고, 아직 구현되지 않은 기능은 완료 상태처럼 표시하지 않는다.

---

## 5. 명시적으로 미루는 것

- 전역 멀티 워커 난사형 병렬  
- 대화 전용 파이프라인과 지시 전용 파이프라인의 조기 분리  
- 에이전트 간 자동 컨텍스트 공유  
- exactly-once 부작용 보장  
- 분산 큐 (Redis 등)를 전제로 한 설계  

현재 전제(Tailscale 개인망 원격 · 단일 운영자)에서는 이득 대비 비용이 크다.

---

## 6. 설계 원칙 (로드맵 공통)

1. 단일 운영자의 Tailscale 서버를 현재 배포 기준으로 삼는다.
2. Intake와 Router 런타임은 통합하고, routing은 enqueue 안의 함수로 둔다.
3. 실행 조율은 본체 에이전트가 맡되 어댑터 capability를 따른다.
4. 대화와 작업은 같은 큐에서 처리하고, 필요할 때 priority로 스케줄한다.
5. 채널 계약과 원 요청 자동 게시를 보존한다.
6. 실제 필요 전에는 분산, 별도 큐, 독립 child session을 추가하지 않는다.

---

## 7. 다음 액션 체크리스트

- [ ] job lifecycle와 이벤트/원 요청 자동 게시 계약 확정
- [x] priority job 메타, 구형 DB migration, WAL 및 busy timeout 설정 반영 (운영 잠금 검증은 남음)
- [x] 기존 멘션/기본 선택을 순수한 `route()`와 `RoutingDecision`으로 정리
- [x] priority 기본값 90과 FIFO tie-break를 정하고 단일 worker 스케줄에 반영
- [ ] Claude·Codex·Gemini의 subagent/steer/interrupt/session capability 표 작성
- [ ] 지원이 확인된 본체부터 steer와 위임 경로 구현
- [ ] 실제 병목 확인 뒤 agent/session concurrency 결정
- [ ] 독립 child job 필요가 확인될 때 parent/child 병합 설계
- [ ] status/tree와 retry/DLQ 운영 요구 정의
- [x] README에서 이 정본 문서와 ADR로 연결

---

## 8. 한 줄 요약

> **처리량용 워커 병렬보다,  
> 단일 priority 큐 위에서 본체가 분신으로 일을 나누고 합치는 모델을  
> Tailscale 개인망 원격 channel-cli에 먼저 심는다.**

---

## 9. 전제 용어: Tailscale 개인망 원격 서버 (단일 운영자)

이 로드맵에서 **운영 환경**을 가리킬 때 쓰는 말이다.

| 요소 | 의미 |
|------|------|
| **원격 서버** | Atrium · channel-cli · Claude/Codex/agy가 **개발 노트북이 아닌 서버**에서 상주한다. |
| **Tailscale 개인망** | 공인 인터넷에 큐·API를 열지 않는다. **tailnet 안**에서만 SSH·UI·채널 접근이 가능하다. |
| **단일 운영자** | 트래픽·정책·에이전트 구독의 주체가 사실상 **한 사람**이다. 다테넌트 SaaS가 아니다. |

실사용: 평소에는 Tailscale로 UI만 쓰고, 개발할 때는 같은 망으로 SSH 하거나 키를 넣어 원격 개발한다.  
이 전제 때문에 통합 큐·SQLite·개인 할당 규칙이 우선이다.
