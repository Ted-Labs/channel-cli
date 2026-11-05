# ADR: Intake Queue와 Router 통합

- **상태:** Accepted  
- **날짜:** 2026-10-30  
- **대상:** channel-cli (Atrium 실행 구성요소)  
- **관련:** channel-cli 아키텍처 설계 draft 0.1, `channel-cli-architecture-v2.md`, `channel-cli-roadmap-v2.md`  

---

## 1. 결정 요약

**Intake Queue와 Router를 별도 단계로 분리하지 않고, 하나의 실행 경로로 통합한다.**

구체적으로:

- 요청은 `send` 시점에 라우팅되어 단일 실행 큐에 들어간다.
- 큐는 priority를 가질 수 있다.
- Worker는 에이전트별 동시성 제한과 priority에 따라 실행만 담당한다.
- 본체/분신, parent–child, 세션 정책은 이 결정과 독립적으로 Store/Adapter 계층에서 다룬다.

---

## 2. 맥락

channel-cli는 채널(웹, Telegram 등)과 서버상 AI CLI(Claude, Codex, Gemini/agy 등) 사이의 durable 큐·라우터다.

초기 아키텍처 초안에서는 역할을 이렇게 나눴다.

```text
채널 → Intake Queue(FIFO) → Router(Jev) → Agent Run Queue(priority) → Adapter
```

이후 논의에서 다음이 명확해졌다.

1. 시스템은 **지시/태스크만이 아니라 일반 대화**도 처리한다.
2. 배포·운영 가정은 **Tailscale 개인망 원격 서버 (단일 운영자)** 이다.
3. 핵심 복잡도는 큐 계층 분리가 아니라 **본체 주도 분해·분신·병합**에 있다.
4. 분신은 기본적으로 **같은 세션의 subagent(1번 케이스)** 로 강제하는 방향이다.

이 전제에서 Intake와 Router를 물리·논리적으로 나눌 실익을 재검토했다.

---

## 3. 선택지

### 선택지 A — 분리

```text
Intake(FIFO) → Router → Agent별 priority queue → Adapter
```

- 접수와 라우팅/실행을 분리
- 대기 중 재라우팅, 백프레셔, 라우터 독립 스케일아웃에 유리

### 선택지 B — 통합 (채택)

```text
send 시 라우팅 → 단일 실행 큐(priority) → Worker/Adapter
```

- enqueue 경로에서 agent / priority / tier를 확정
- Worker는 실행·동시성·세션 연결만 담당

### 선택지 C — 하이브리드

- 코드상 함수는 `route()`와 `enqueue_run()`로 분리
- 배포·프로세스는 하나로 유지  
  (인터페이스만 분리, 런타임은 통합)

선택지 C는 구현 전술로 채택 가능하나, **아키텍처 결정의 본문은 B**다.

---

## 4. 결정 근거

### 4.1 Tailscale 개인망 원격 서버 (단일 운영자) 가정

현재 목표 환경은 다음과 같다.

- 단일 운영자(Ted) 중심
- 원격 서버상 SQLite 큐 (개발 노트북이 아닌 상주 서버)
- Tailscale tailnet 안에서만 접근 (공인 공개 없음)
- 초당 대량 유입이 아닌 대화·작업 혼합 트래픽
- 운영자가 곧 개발자 (SSH·키로 원격 개발)

이 규모에서는 “일단 접수만 하고 라우팅은 비동기로” 패턴의 이득이 작다.  
대신 컴포넌트 수, 상태 전이, 장애 지점, 디버깅 면이 늘어난다.

### 4.2 대화 지연이 중요하다

시스템은 지시 전용이 아니다. 일반 대화도 동일 경로를 탄다.

대화에 필요한 것은:

- 낮은 응답 지연
- 같은 `(channel, room, agent)` 세션으로의 빠른 연결
- “사용자가 기다리는 요청”의 우선 처리

Intake에 쌓였다가 Router를 한 번 더 거치는 구조는 대화 경로를 길게 만든다.  
통합 시 `send → 라우팅 → 해당 실행 슬롯`이 한 줄로 끝난다.

### 4.3 라우팅 자체가 가볍다

현 단계의 라우팅 입력은 대체로:

- 명시 멘션 (`@claude` 등)
- room의 기존 세션/기본 에이전트
- 요청 종류 힌트
- (선택) 간단한 부하·큐 길이

외부 모델 호출이나 수 초짜리 의사결정이 아니다.  
가벼운 결정을 위해 큐를 하나 더 두는 것은 비용 대비 효과가 낮다.

### 4.4 진짜 복잡도는 다른 곳에 있다

제품의 차별점과 난이도는 아래에 있다.

- 본체(frontier) vs 분신(하위 tier)
- in-session subagent 강제
- parent/child job과 병합
- 세션·권한 경계
- 채널 events 계약

큐를 Intake/Router로 나누는 것은 이 핵심을 단순화하지 않는다.  
오히려 주의와 구현 예산을 분산시킨다.

### 4.5 Priority로 대화와 지시를 구분할 수 있다

통합 큐에서도  suffices:

| 유형 | priority 예 |
|------|-------------|
| 사용자 대면 대화 | 높음 |
| 본체 주 작업 | 중상 |
| 분신·백그라운드 | 낮음 |

“대화라서 파이프라인을 분리해야 한다”는 요구는,  
현재 규모에서는 priority와 세션 연속성으로 충족 가능하다.

---

## 5. 분리안이 더 나아지는 조건 (비채택 이유와 재검토 트리거)

아래가 **실제로** 발생하면 본 ADR을 재검토하고 분리를 고려한다.

1. 유입량이 실행 처리량을 상회해, 접수와 실행의 백프레셔 분리가 필요해질 때  
2. 라우팅이 외부 API·모델·장기 상태 조회에 의존해 비용이 커질 때  
3. queued 상태에서 부하에 따른 **재라우팅**이 자주 필요할 때  
4. Router/Worker를 프로세스·머신 단위로 나눠야 할 때  
5. 대화 경로와 대량 배치 지시 경로를 운영상 완전히 분리해야 할 때  

현재 가정(Tailscale 개인망 원격 · 단일 운영자)에서는 위 조건이 충족되지 않는다.

---

## 6. 결과적 아키텍처 (본 결정 반영)

```text
[채널 / Ted]
    │ send
    ▼
[라우팅 포함 enqueue]     ← agent, priority, tier 확정
    │
    ▼
[단일 실행 큐]            ← priority + created_at
    │
    ▼
[Worker]                  ← agent/session 동시성 limit
    │
    ▼
[Adapter]                 ← Claude / Codex / Gemini
    │
    ├── 본체 세션
    └── 분신 = in-session subagent (기본)
```

**상태 모델 단순화**

- 분리 시: `received → routed → running → …`
- 통합 시: `queued → running → waiting_children? → done | failed | dead_letter | interrupted`

---

## 7. 구현 시 권장 전술

런타임은 통합하되, 코드 경계는 나중에 찢기 쉽게 둔다.

```text
route(job_input) -> RoutingDecision
enqueue(job, decision) -> job_id
worker_pick() -> job
execute(job) -> result
```

지금은 한 프로세스에서 연속 호출하고,  
재검토 트리거가 오면 `route`와 `enqueue` 사이에 큐를 삽입하면 된다.

---

## 8. 영향을 받지 않는 결정

본 결정은 다음을 바꾸지 않는다.

- 채널 계약 (`send` / `events` / job_id)
- 세션 키 `(channel, room, agent)`
- 본체 frontier / 분신 하위 tier 정책
- 분신 기본 경로 = in-session subagent
- SQLite Store(서버 디스크), 외부 의존성 최소화
- exactly-once를 과도하게 약속하지 않는 태도

---

## 9. 결론

**Tailscale 개인망 원격 서버 (단일 운영자)** 이고, **대화와 지시가 공존**하며, **핵심 복잡도가 본체/분신 모델**에 있는 현재 단계에서는  
Intake Queue와 Router를 분리하는 것보다 **통합하는 편이 단순하고 지연에 유리하다.**

확장성 일반론에서는 분리가 더 잘 버티지만,  
그 확장 축이 실제 통증이 되기 전에는 통합을 유지한다.

---

## 10. 결정 한 줄

> **접수 큐와 라우터를 나누지 않고,  
> send 시 라우팅되는 단일 priority 실행 큐로 간다.**

---

## 11. 전제 용어: Tailscale 개인망 원격 서버 (단일 운영자)

| 요소 | 의미 |
|------|------|
| **원격 서버** | Atrium · channel-cli · 실행 CLI가 개발 노트북이 아닌 **서버에서 상주**한다. |
| **Tailscale 개인망** | 공인 인터넷에 열지 않고, **tailnet 안**에서만 SSH·UI·채널 접근이 가능하다. |
| **단일 운영자** | 트래픽·정책·구독의 주체가 사실상 **한 사람**이다. 다테넌트 SaaS가 아니다. |

이 전제 하에서 본 ADR의 통합 결정이 성립한다.
