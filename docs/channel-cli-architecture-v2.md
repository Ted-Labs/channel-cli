# channel-cli 아키텍처 설계 v2

- **버전:** 2.0  
- **날짜:** 2026-10-30  
- **이전:** draft 0.1 (`channel-cli-architecture.md`)  
- **전제:** Ted-Labs/channel-cli (Atrium 실행 구성요소), **Tailscale 개인망 원격 서버 (단일 운영자)**  
- **관련 ADR:** `adr-intake-router-integration.md` (Intake + Router 통합)
- **문서 상태:** 정본. v1 초안은 `archive/`에 보존한다.

---

## 0. v1 → v2 변경 요약

| 항목 | v1 | v2 |
|------|----|----|
| Intake / Router | 분리 (FIFO → Jev → Agent queue) | **통합** (send 시 라우팅 → 단일 priority 큐) |
| 병렬의 의미 | 에이전트별 워커 병렬 비중 큼 | **본체 주도 분해·위임·병합**이 중심 |
| 분신 | in-session 기본 + 옵션 child job | 본체별 지원 확인 후 in-session 우선, 미지원은 보류/대안 |
| 운영 가정 | 로컬·개인 | **Tailscale 개인망 원격 서버 (단일 운영자)**, 분산은 후순위 |
| 대화 vs 지시 | 언급만 | 동일 경로, **priority로 구분** |

---

## 현재 코드 상태 (작업 브랜치, 2026-11-01)

이 문서는 목표 아키텍처다. 아래 표는 이 작업 브랜치의 코드 상태이며 운영 배포 상태를 뜻하지 않는다.

| 영역 | 현재 운영 상태 | 목표와의 차이 |
|------|----------------|---------------|
| 큐·워커 | 단일 SQLite 큐, priority 내림차순 + 접수 순서, 전역 worker 1개 | agent/session 동시성 제한 없음 |
| 라우팅 | 결정적 `RoutingDecision(agent, prompt, priority)`를 enqueue 시 저장; 기본 priority 90, `send --priority`로 0..100 지정 | 요청 종류별 자동 tier/priority 정책은 없음 |
| 실행 중단 | Codex·Cora·Gemini body 작업의 기본 interrupt가 운영 반영됨 | 긴급 대화 전달을 위한 steer와 interrupt 정책은 별도 |
| job 구조 | 기본 job 상태, attempt별 interrupt 기록, priority 및 기존 DB migration | tier/parent/root/spawn_mode 및 child 병합 없음 |
| 세션 | `(channel, room, agent)`별 네이티브 세션 | 에이전트별 capability 및 점유 정책을 더 명시해야 함 |

운영 환경과 이 에이전트 작업 환경은 a1-2다. 같은 서버 안의 작업본에서 검증·반영할 때 SSH를 기본 단계로 전제하지 않는다. 배포 방식은 실제 변경 대상에 맞는 저장소의 운영 절차를 따른다.

## 1. 목표

한 사람(Ted)의 요청이 Channel CLI로 들어오면,

1. **어디에 / 누구에게 / 어떤 종류로** 보낼지 정하고 (enqueue 시 라우팅)
2. 해당 에이전트(**본체**)가 작업을 수행하며
3. 필요 시 **같은 세션 안의 분신(subagent)** 으로 일을 나누고
4. 본체가 결과를 병합·검증한 뒤
5. 채널에 완료를 돌려준다

인프라 워커가 일을 나눠 돌리는 구조가 아니라,  
**권한을 위임받은 본체가 큐를 이용해 스스로 분해·위임·수거하는 구조**가 중심이다.

시스템은 **지시만이 아니라 일반 대화**도 같은 경로로 처리한다.

| 구분 | 모델 수준 | 역할 |
|------|-----------|------|
| **본체** | 프론티어급 | 분해, 위임 판단, 병합, 최종 검증, 완료 신호 |
| **분신** | 1~2단계 아래 | 좁고 구체적인 하위 실행 (어댑터가 지원하면 in-session subagent 우선) |

---

## 2. 전체 구조 (v2)

```text
[채널 / Ted]
      │ send
      ▼
[Channel CLI: enqueue 안에서 route() 결정]
      │ RoutingDecision
      ▼
[단일 실행 큐 + SQLite Store]
      │ priority / FIFO tie-break (목표)
      ▼
[Worker: 현재 1개, 이후 필요 시 agent/session limit]
      │
      ▼
[Adapter] ── 본체 세션
      └──────── 지원 시 in-session subagent

Router는 별도 큐나 프로세스가 아니라 enqueue 시 호출하는 함수다.
```

**한 줄:**  
접수 큐와 라우터를 나누지 않고, **send 시 라우팅되는 단일 priority 실행 큐**로 간다.

---

## 3. 계층별 역할

### 3.1 단일 실행 큐 + enqueue 시 라우팅

**역할**

- 채널 요청 접수, `message-id` 중복 처리
- **enqueue 시점에** agent / priority / tier 확정 (Jev)
- 실행 대기열 유지 (priority + created_at)
- 채널 events의 원천 job 관리

**상태 (단순화)**

```text
queued → running → waiting_children? → done | failed | dead_letter | interrupted
```

- `waiting_children`: 기본 정책인 `in-session subagent`에서는 발생하지 않으며, 예외적인 `spawn_mode: session`(별도 child job)인 경우에만 발생한다.
- `dead_letter`: 재시도 한도(`max_attempts`)를 초과하여 최종 실패 처리된 상태.
- v1의 `received → routed` 구분은 두지 않는다.

**주요 job 필드**

```text
job_id
channel, room, message_id
raw_body
state
agent                 # enqueue 시 확정
tier: body | worker
priority              # 0..100
kind: user | system | child
parent_job_id?
root_job_id
spawn_mode?: subagent | session   # 기본 subagent
created_at, updated_at
```

**priority 가이드**

| 점수대 | 예시 |
|--------|------|
| 90–100 | 사용자 대면 대화, 본체 최종 병합·검증 |
| 70–89  | 본체 주 작업·지시 |
| 40–69  | 일반 하위·중간 작업 |
| 0–39   | 백그라운드, 재시도, 저우선 분신 기록 |

대화와 지시는 **파이프라인 분리 없이** priority로 구분한다.

---

### 3.2 Router (Jev) — 단계가 아니라 함수

물리 큐가 아니라 **enqueue 경로의 결정 로직**이다.

입력 예:

- 멘션 / 기본 에이전트
- 요청 종류 (대화, 개발, 지시)
- room 기존 세션
- (선택) 에이전트 큐 길이·간단한 부하

목표 출력 (`RoutingDecision` 스펙):

```python
class RoutingDecision:
    agent: str              # claude | codex | gemini | ...
    tier: str               # body (frontier) | worker (하위 tier)
    priority: int           # 0..100 (대화: 90~100, 본체 주작업: 70~89, 분신: 0~39)
    spawn_mode: str         # subagent | session (기본: subagent)
    cwd: Optional[str]      # 실행 경로 정책
    literal: bool = False   # 멘션 재해석 여부
```

**규칙 초안**

1. 명시 멘션 최우선  
2. 같은 room이면 기존 agent 세션 유지 경향  
3. 종류·역할 가중치  
4. 난이도·규모 → body(frontier) vs worker tier  
5. 동점이면 FIFO (`created_at`)

코드 경계는 `route()` / `enqueue()`로 나누어,  
나중에 분리 ADR 재검토 시 큐를 끼우기 쉽게 둔다.  
→ 자세한 근거: `adr-intake-router-integration.md`

---

### 3.3 Worker

- 단일 실행 큐를 소비
- **agent 또는 (agent, session) 단위 동시성 limit**
- **같은 본체 세션으로 가는 작업은 직렬**이 기본
- 권한이 분리된 작업만 제한적 동시 실행
- **세션 직렬화와 긴급 대화(Steer/Interrupt):**
  - 본체 세션이 긴 작업(지시 태스크)을 수행 중일 때 우선순위가 높은 대면 대화(90~100, 예: "중단", "상황 보고")가 인큐되면, 일반 큐 대기로 인한 Head-of-Line Blocking을 방지하기 위해 해당 세션의 **Body 모드 Steer/Interrupt 인터페이스**로 즉시 신호를 전달하거나 우선 처리한다.
- **작업 타임아웃:** 세션 점유 독점을 막기 위해 작업 단위의 `job_timeout` 및 무응답 방지 정책을 둔다.
- 기존 `channel worker` / `--once` / `--drain` 진입점 유지

인프라 수준의 “에이전트별 워커 난사”는 목표가 아니다.

---

### 3.4 Adapter

| Adapter | 본체 | 분신(기본) |
|---------|------|------------|
| Claude  | 세션 / body | 지원 여부를 검증한 뒤 in-session subagent 우선 |
| Codex   | 세션 / codex-body | 실제 app-server subagent 계약 확인 전 미정 |
| Gemini(agy) | 세션 / body | 실제 CLI subagent 계약 확인 전 미정 |

**위임 정책**

- top-level job은 기본적으로 본체 하나다.
- in-session subagent를 우선 검토하되 본체 CLI가 지원하는 기능·결과 회수 계약을 확인한 뒤 활성화한다.
- 본체가 지원하지 않는 경우 독립 세션을 자동으로 열지 말고 명시적 정책 결정 전까지 위임을 보류한다.
- 독립 session child job은 분리된 권한·수명 주기가 실제로 필요할 때만 예외로 추가한다.

---

### 3.5 Store (SQLite)

기존 `~/.local/state/channel` 확장.  
CLI 프로세스(`atrium send`)와 Worker의 동시 접근을 위해 WAL을 사용한다. 연결은 `sqlite3.connect(timeout=15)`와 `PRAGMA busy_timeout=15000`을 명시해 15초를 유지한다. 이는 기존 동작을 보존하기 위한 S1 결정이며, 운영 데이터에서 잠금 대기 문제가 관측되면 별도 조정한다.

- **jobs** — priority, tier, parent/root, spawn_mode, retry_count, state (`queued` | `running` | `waiting_children` | `done` | `failed` | `dead_letter` | `interrupted`)  
- **job_links** — parent ↔ children (관측·예외 child job용)  
- **sessions** — `(channel, room, agent)` → native session id  
- **events** — 채널 폴링 계약 유지  
- **agent_runtime** — 큐 길이, running, 최근 사용량(선택)

---

## 4. 핵심 실행 흐름

### 4.1 대화 또는 단일 지시

```text
send
 → route(agent, priority, tier)
 → 단일 큐 enqueue
 → worker → adapter → 본체 세션
 → done + event
```

### 4.2 큰 태스크 (권장: 본체 + in-session 분신)

```text
1. 큰 태스크 → route → Claude 본체 (frontier, high priority)
2. 본체 세션이 N개로 분해
3. 분신 = in-session subagent (하위 tier)
4. 본체 자신 몫 처리
5. 결과 회수 → 병합 → 테스트
6. 본체만 최종 answer 반환 → job done + event
```

큐에 보이는 top-level job은 본체 하나다.

### 4.3 예외: 독립 세션 child job

긴 수명·다른 에이전트·명시적 session spawn이 필요할 때만:

1. **Child Job 생성:** 본체 에이전트가 도구(Tool)를 통해 `channel send --parent-job-id <id> --spawn-mode session`으로 독립 실행 요청을 전달.
2. **부모 대기:** 부모 job의 상태가 `waiting_children`으로 전이.
3. **완료 및 Wakeup 트리거:**
   - Worker가 마지막 child job을 완료(`done` 또는 `failed`) 처리할 때, Store 트랜잭션 내에서 동일 `parent_job_id`를 가진 모든 자식들의 종료를 확인.
   - 모든 자식이 완료되면 부모 job의 상태를 `waiting_children`에서 `queued`(우선순위 95, 병합 단계)로 자동 전이시켜 본체 세션을 다시 깨운다.
4. **결과 병합:** 본체가 자식들의 결과를 취합·테스트 후 최종 답장 반환 → root `done` + event.

```text
parent (waiting_children)
  ├─ child jobs (worker tier, lower priority)
  └─ 본체 몫
→ 마지막 child 완료 시 Worker가 parent를 queued(priority 95)로 Wakeup
→ parent 병합 재진입 → root done
```

채널에는 **root job 최종 결과**만 노출하는 것을 기본으로 한다.

---

## 5. 채널 계약

기존 계약을 유지한다.

1. `send --json` → `job_id`  
2. `worker` 처리  
3. `events --json --after <cursor>`에서 종료 상태를 읽는다.  
4. 채널 어댑터가 `done.answer`를 원 요청에 한 번 게시한다. Atrium 웹 공통 큐에서는 큐가 자동 게시하므로 에이전트/작업자가 `atrium reply`로 같은 최종 답을 중복 게시하지 않는다.  

선택 확장: payload에 `root_job_id`, `phase` (`queued` | `running` | `waiting_children` | `merging` | `done`).

---

## 6. 메모리·세션

| 종류 | 범위 | 기본 |
|------|------|------|
| 세션 컨텍스트 | `(channel, room, agent)` | CLI 네이티브 세션 |
| 작업 단기 기억 | job / root | Store + 로그 |
| 영구·공용 메모리 | 후속 레이어 | 기본 off, 명시적 공유만 |

에이전트 간 맥락 **자동 공유 없음**.  
본체–분신 공유는 같은 세션 울타리 안에서만.

---

## 7. 설계 원칙

1. **Tailscale 개인망 원격 서버 (단일 운영자)** 를 기본 가정으로 한다.  
2. **Intake와 Router는 통합**한다 (ADR 참고).  
3. **권한·조율의 주인은 본체 에이전트**다.  
4. **분신은 본체가 지원하면 in-session subagent를 우선한다.**  
5. **프론티어 = 본체, 하위 tier = 분신**.  
6. **대화와 지시는 같은 큐**, priority로 구분한다.  
7. **에이전트 간 자동 컨텍스트 공유 금지**.  
8. **채널 JSON 계약 하위 호환**.  
9. **외부 의존성 최소화** (SQLite).  
10. **exactly-once를 과도하게 약속하지 않는다**.

---

## 8. 분리로 되돌리는 재검토 트리거

다음이 **실제로** 통증이 되면 Intake/Router 분리를 재검토한다.

- 유입 ≫ 실행, 접수와 실행 백프레셔 분리 필요  
- 라우팅이 외부·장기 조회에 의존  
- queued 상태에서 재라우팅이 빈번  
- Router/Worker 프로세스·머신 분리 필요  
- 대화 경로와 대량 배치 경로의 운영 분리 필요  

그 전에는 통합을 유지한다.

---

## 9. 컴포넌트 맵 (코드)

```text
channel_cli/
  cli.py
  store.py          # jobs, links, sessions, events
  router.py         # Jev (함수, 단독 큐 아님)
  queue.py          # 단일 priority 실행 큐
  worker.py
  adapters.py
  body.py / claude_body.py / agy_body.py
  web.py
  output.py
```

**관측 명령 (목표)**

```text
channel status
channel jobs --tree     # 지원되는 in-session 분신은 단일 Root job으로 표기, session child를 도입한 뒤 트리를 렌더링
channel queues          # priority/agent별 대기 요약
```

---

## 10. 단계적 도입

| 단계 | 내용 |
|------|------|
| **S0 — 현재** | 단일 priority 큐 + 단일 worker. 높은 priority 우선, 동점은 FIFO. 기본 priority 90. body 기본 interrupt 운영 반영. |
| **S1 — P0 기반** | `jobs.priority`와 구형 DB migration, WAL 및 15초 busy timeout 명시를 반영했다. 잠금 대기 운영 검증은 아직 별도다. |
| **S2 — P0 라우팅** | 순수한 `route()`가 기존 멘션/기본 선택을 `RoutingDecision(agent, prompt, priority)`로 반환하고 enqueue 시 저장한다. 외부 모델 호출 없이 결정적이다. tier는 정책이 정의될 때 추가한다. |
| **S3 — P0 스케줄** | priority 내림차순과 `seq` 오름차순 tie-break를 적용했다. 단일 worker를 유지하며 동시성은 실제 병목과 세션 안전성 확인 뒤 별도 단계로 둔다. |
| **S4** | 실제 병목 확인 후 agent/session 동시성 limit을 결정한다. |
| **S5** | 본체별 분신 capability를 조사하고 지원되는 어댑터에만 위임한다. |
| **S6** | 독립 session child가 필요할 때 parent/child와 병합 재진입을 추가한다. |
| **S7** | status/tree와 retry/DLQ 등 운영 관측·복구를 구현한다. |
| **S8** | ADR 재검토 조건이 충족될 때만 Intake/Router 런타임 분리를 논의한다. |

Intake와 Router를 별도 런타임으로 나누는 것은 기본 단계가 아니다.

---

## 11. 한 줄 요약

> **Tailscale 개인망 원격 서버 위의 channel-cli는  
> send 시 라우팅되는 단일 priority 큐 위에서  
> 프론티어 본체가 in-session 분신으로 일을 나누고  
> 병합·검증 후 채널에 완료를 돌려주는 구조로 간다.**

---

## 12. 전제 용어: Tailscale 개인망 원격 서버 (단일 운영자)

이 문서에서 **운영 환경**을 가리킬 때 쓰는 말이다. 뜻은 다음과 같다.

| 요소 | 의미 |
|------|------|
| **원격 서버** | Atrium UI · channel-cli 워커 · Claude/Codex/agy 등 실행 CLI가 **개발 노트북이 아닌 서버**에서 상주한다. |
| **Tailscale 개인망** | 공인 인터넷에 큐·API를 열지 않는다. **tailnet 안**에서만 SSH·UI·채널이 접근 가능하다. |
| **단일 운영자** | 트래픽·정책·에이전트 구독의 주체가 사실상 **한 사람(Ted)** 이다. 다테넌트 SaaS가 아니다. |

실사용 패턴:

- 평소: Tailscale로 UI/채널만 연결해 대화·칠판·작업을 돌린다.  
- 개발: 같은 tailnet으로 SSH 하거나, 키·설정만 넣어 두고 서버에 붙여 원격 개발한다.  
- 보안 경계: Tailscale이 사실상 방화벽이다. 외부 공개 배포를 전제로 한 설계(강제 분산 큐, 테넌트 격리 등)는 하지 않는다.

이 전제 때문에 Intake/Router **통합**, SQLite 단일 큐, 개인 usage·지망·sticky 규칙이 우선이고,  
공개 서비스급 스케일아웃은 **재검토 트리거가 올 때까지** 후순위로 둔다.
