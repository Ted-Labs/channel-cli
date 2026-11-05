> **이 문서는 보관용 v1 초안이며 정본이 아닙니다. 최신 문서는 [channel-cli-architecture-v2.md](../channel-cli-architecture-v2.md)를 따릅니다.**

# channel-cli 아키텍처 설계

**버전:** draft 0.1 (Deprecated)  
**날짜:** 2026-10-30  
**전제:** Ted-Labs/channel-cli (Atrium 실행 구성요소)를 확장하는 방향

---

## 1. 목표

한 사람(Ted)의 요청이 Channel CLI로 들어오면,

1. **어디에 / 누구에게 / 어떤 종류로** 보낼지 정하고
2. 해당 에이전트(본체)가 작업을 수행하며
3. 필요 시 **같은 세션 안의 분신(subagent)** 으로 일을 나누고
4. 본체가 결과를 병합·검증한 뒤
5. 채널에 완료를 돌려준다

인프라 워커 병렬이 아니라, **본체 주도 분해·위임·병합**이 중심이다.

| 구분 | 모델 | 역할 |
|------|------|------|
| 본체 | 프론티어급 | 분해, 위임, 병합, 최종 검증, 완료 신호 |
| 분신 | 1~2단계 아래 | 좁은 하위 작업 실행 (기본: in-session subagent) |

---

## 2. 전체 구조

```text
                    ┌─────────────┐
                    │  나, Ted    │
                    │ (웹/TG/CLI) │
                    └──────┬──────┘
                           │ send / events
                    ┌──────▼──────┐
                    │ Channel CLI │
                    │  (Atrium)   │
                    └──────┬──────┘
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ▼              ▼              ▼
      ┌──────────┐  ┌──────────┐  ┌──────────┐
      │ Intake   │  │  Router  │  │  Store   │
      │ Queue    │→ │  (Jev)   │  │ (SQLite) │
      │ (FIFO)   │  └────┬─────┘  └────▲─────┘
      └──────────┘       │             │
                         │ route       │ parent/child,
                         ▼             │ sessions, events
              ┌─────────────────────┐  │
              │  Agent Run Queues   │──┘
              │  (priority / agent) │
              └──────────┬──────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
    ┌─────────┐    ┌─────────┐    ┌─────────┐
    │ Adapter │    │ Adapter │    │ Adapter │
    │ Claude  │    │  Codex  │    │ Gemini  │
    └────┬────┘    └────┬────┘    └────┬────┘
         │              │              │
         ▼              ▼              ▼
      본체 세션       본체 세션       본체 세션
         │
         ├── subagent (분신)  ← 기본 위임 경로 (1번 케이스 강제)
         └── (옵션) child job ← 별도 세션이 꼭 필요할 때만
```

---

## 3. 계층별 역할

### 3.1 Intake Queue (중앙, FIFO)

**역할:** 채널에서 들어온 요청의 “접수 순서”를 보장한다.

- 채널·room·message-id·본문·요청 메타를 저장
- 중복 `message-id` 처리 (기존과 동일)
- 라우팅 전 대기열
- 이벤트 커서의 기준이 되는 원천 job 생성

**우선순위 없음(또는 매우 약함).**  
“누가 먼저 말했는가”를 중앙이 지킨다.

**주요 필드 (job)**

```text
job_id
channel, room, message_id
raw_body
state: received | routed | running | waiting_children | done | failed | interrupted
parent_job_id?          # 하위 job일 때만
root_job_id             # 병합 기준
agent?                  # 라우팅 후 확정
priority?               # 에이전트 큐에서 사용
kind: user | system | child
created_at, updated_at
```

---

### 3.2 Router (Jev)

**역할:** 요청을 보고 실행 경로를 정한다.

입력 예:

- 목적지 힌트 (개발 / SNS / 일반 대화 등)
- 멘션 (`@claude`, `@codex` …) 또는 기본 에이전트
- 요청 종류 (대화, 개발, 지시)
- 현재 에이전트 부하·잔여 토큰·쌓인 작업 수 (Store 조회)

출력:

```text
agent: claude | codex | gemini | ...
tier: body | worker          # 본체 / 분신 등급
priority: 0..100
cwd / room 정책
literal?                     # 멘션 재해석 여부
```

**규칙 초안**

1. 명시 멘션이 있으면 최우선
2. 종류·역할 가중치 (개발 → codex/claude, 이미지 → 해당 에이전트 등)
3. 에이전트별 잔여 용량 (토큰, 큐 길이)
4. 난이도 높음 → 본체(frontier), 낮고 쪼개진 일 → worker tier
5. 동점 시 FIFO (intake 순서)

Router는 **실행하지 않는다.**  
Intake job을 읽어 Agent Run Queue에 넣고, Store에 라우팅 결과만 기록한다.

---

### 3.3 Agent Run Queue (에이전트별 Priority Queue)

**역할:** 이미 “이 에이전트로 간다”고 정해진 작업의 실행 순서와 동시성을 관리한다.

- 키: `agent` (필요 시 `agent + room`)
- 정렬: `priority DESC`, 동점이면 `created_at ASC`
- 동시성: 에이전트(또는 세션) 단위 limit  
  - 같은 본체 세션으로 가는 작업은 **직렬**이 기본  
  - 권한이 분리된 작업만 제한적 병렬

**priority 가이드**

| 점수대 | 예시 |
|--------|------|
| 90–100 | 사용자 대면, 본체 최종 병합/검증 |
| 70–89  | 본체 주 작업 |
| 40–69  | 일반 하위 작업 |
| 0–39   | 백그라운드, 재시도, 저우선 분신 |

---

### 3.4 Adapter

**역할:** 각 CLI의 네이티브 세션·권한·입출력에 맞춘 실행.

| Adapter | 본체 실행 | 분신(기본) | 비고 |
|---------|-----------|------------|------|
| Claude  | 세션 resume / body | **in-session subagent 강제** | session id 추적 부담 최소 |
| Codex   | 세션 / codex-body | sub 또는 child job | 기존 body 경로 활용 |
| Gemini(agy) | 세션 / body | 정책에 따라 | 별칭 유지 |

**Claude 분신 정책 (1번 케이스 강제)**

- 본체가 하위 일을 나눌 때 → Claude Agent/subagent 경로만 사용
- 새 `claude` 프로세스·독립 session으로 분신을 만들지 않음
- 부모는 agent id + 요약 결과를 받아 병합
- channel-cli는 “분신 job”을 별도 top-level session job으로 올리지 않음  
  (관측용 child record만 Store에 남기는 것은 가능)

예외적으로 독립 세션 child job이 필요하면 `spawn_mode: session`을 명시할 때만 허용.

---

### 3.5 Store (SQLite, 기존 확장)

기존 `~/.local/state/channel` 구조를 확장한다.

**추가·강조 엔티티**

1. **jobs** — intake + 실행 상태, parent/root, priority, tier  
2. **job_links** — parent ↔ children  
3. **sessions** — `(channel, room, agent)` → native session id (기존)  
4. **events** — 채널 폴링용 (기존 계약 유지)  
5. **agent_runtime** — 큐 길이, running 수, 최근 토큰/비용, 헬스  

**부모–자식 상태**

```text
root/parent: running
  child A: done
  child B: running
  child C: done
→ parent: waiting_children
→ 모두 done → parent 다시 runnable (병합 단계)
→ 병합·테스트 성공 → done + event
```

---

## 4. 핵심 실행 흐름

### 4.1 사용자 요청 (단일 에이전트, 분신 없음)

```text
1. channel send
2. Intake Queue에 job 생성 (state=received)
3. Router가 agent/priority 결정 → Agent Run Queue
4. Worker가 adapter로 실행
5. 결과 저장, events에 done
6. 채널이 events로 읽어 답장
```

### 4.2 큰 태스크 + 본체 분해 (권장 경로)

```text
1. 큰 태스크 → Intake → Router → Claude 본체 (frontier, high priority)
2. 본체 세션이 작업을 3개로 분해
3. 분신 2개는 in-session subagent로 실행 (낮은 tier 모델)
4. 본체는 나머지 1개 처리
5. subagent 결과 회수 → 병합 → 테스트
6. 본체만 channel-cli에 최종 answer 반환
7. job done + event
```

이 경로에서 큐에 보이는 top-level job은 **본체 job 하나**가 기본이다.  
분신은 Claude 세션 내부 실행으로 취급한다.

### 4.3 (옵션) 분신을 child job으로 올리는 경우

독립 세션·다른 에이전트·긴 수명이 필요할 때만:

```text
본체 job (waiting_children)
  ├─ child job → Agent Run Queue (worker tier, lower priority)
  ├─ child job → ...
  └─ 본체 자신 몫
모두 완료 후 본체 병합 job이 다시 큐에 진입
```

Store에 `parent_job_id` / `root_job_id`로 묶고, 채널에는 **root job의 최종 결과만** 노출하는 것을 기본으로 한다.

---

## 5. Worker 모델

- **논리적 워커:** agent run queue를 소비
- **기본 동시성:**  
  - 전역으로 무제한 난사하지 않음  
  - `agent` 또는 `(agent, session)` 단위 limit  
  - **같은 본체 세션 작업은 직렬**
- Intake → Router는 가벼운 별도 루프여도 됨 (실행 워커와 분리 가능)

기존 `channel worker`는 이 루프의 CLI 진입점으로 유지한다.

```text
channel worker           # intake 라우팅 + agent queue 소비
channel worker --once
channel worker --drain
```

---

## 6. 채널 계약 (유지·확장)

기존 계약을 깨지 않는다.

1. `send --json` → `job_id` 반환  
2. `worker`가 처리  
3. `events --json --after <cursor>` 로 상태 수신  
4. `done`의 `answer`를 방에 게시  

**확장 (내부)**

- event payload에 `root_job_id`, `phase` (`routed` | `running` | `waiting_children` | `merging` | `done`) 선택 포함
- 채널은 여전히 root 완료만 사용자에게 보여도 됨

---

## 7. 메모리·세션 정책

다이어그램의 “대화 내역 / 컨텍스트 / 영구·공용 메모리”는 이렇게 나눈다.

| 종류 | 범위 | 저장 |
|------|------|------|
| 세션 컨텍스트 | `(channel, room, agent)` | 각 CLI 네이티브 세션 |
| 작업 단기 기억 | job / root_job | Store + 로그 |
| 영구 메모리 | 사용자·프로젝트 정책 | 별도 메모리 레이어 (후속) |
| 공용 메모리 | 에이전트 간 공유가 필요할 때만 | 명시적 공유 옵션 (기본 off) |

**기본값: 에이전트 간 맥락 자동 공유 없음** (현행 유지).  
본체–분신(1번 케이스)은 같은 세션 울타리 안에서만 공유된다.

---

## 8. 컴포넌트 맵 (코드 관점)

기존 패키지 `channel_cli` 기준 확장 스케치.

```text
channel_cli/
  cli.py              # atrium/channel 진입점
  store.py            # jobs, links, sessions, events, agent_runtime
  intake.py           # FIFO intake enqueue/dequeue
  router.py           # Jev 라우팅
  run_queue.py        # agent별 priority queue 연산
  worker.py           # 루프: route + run
  adapters.py         # claude/codex/agy/opencode
  body.py             # 기존 body 경로
  claude_body.py
  agy_body.py
  web.py              # 웹 reply/share 등
  output.py
```

**최소 추가 명령**

```text
channel status              # intake / agent queue / running
channel jobs --tree         # parent-child 트리
channel agent-queues        # 에이전트별 대기열·priority
```

---

## 9. 설계 원칙 (고정)

1. **권한의 주인과 실행의 주인은 본체 에이전트**  
2. **중앙 큐는 접수·순서, 에이전트 큐는 실행 스케줄**  
3. **분신은 기본 in-session subagent (1번 케이스)**  
4. **프론티어 = 본체, 낮은 tier = 분신**  
5. **에이전트 간 자동 컨텍스트 공유 금지**  
6. **채널 계약(JSON events) 하위 호환**  
7. **외부 의존성 최소화 (로컬 SQLite 유지)**  
8. **exactly-once를 과도하게 약속하지 않음**

---

## 10. 단계적 도입

| 단계 | 내용 |
|------|------|
| **S0** | 현행 유지: 단일 FIFO + 단일 워커 |
| **S1** | job에 `priority`, `parent_job_id`, `root_job_id` 추가. Store 확장 |
| **S2** | Intake / Agent Run Queue 분리. Router(Jev) 최소 규칙 |
| **S3** | Claude adapter에 subagent 위임 정책 명시 (1번 강제) |
| **S4** | `waiting_children` + 병합 재진입 |
| **S5** | status/tree 관측, tier·비용 추적 |
| **S6** | (선택) 독립 session child job, 분산 등 |

---

## 11. 한 줄 요약

> **Channel CLI는 접수용 FIFO와 라우터를 중심에 두고,  
> 에이전트별 priority 실행 큐로 본체를 돌리며,  
> 분신은 같은 세션 subagent로 강제해  
> 본체가 분해·병합·완료 신호까지 책임지는 구조로 간다.**

이 설계는 기존 channel-cli의 채널 계약·세션 분리·body 모드를 깨지 않으면서,  
다이어그램의 Jev·다중 에이전트·본체/분신 모델을 수용하기 위한 목표 아키텍처이다.
