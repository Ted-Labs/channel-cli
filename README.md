# Atrium 실행 구성요소

Atrium은 한 사람의 주 에이전트를 중심으로 여러 에이전트를 연결하는 공통 플랫폼이다. 이 패키지는 Atrium의 독립 실행 구성요소이며, 여러 채널에서 설치된 에이전트 CLI를 하나의 로컬 큐로 호출한다. 웹·Telegram 등의 채널은 메시지를 넣고 결과 이벤트를 읽으며, 에이전트 선택과 세션 관리는 이 CLI가 담당한다.

Python 3.11 이상, macOS/Linux에서 동작한다. 런타임 외부 의존성은 없다.

```sh
uv tool install --editable .
atrium agents
channel send --channel web --room dev --message-id web-123 '@agy 안녕'
channel worker --once
channel jobs
channel result <job-id>
```

`atrium`이 기본 명령이며 기존 `channel` 명령도 같은 실행 구성요소를 호출하는 호환 별칭으로 유지한다.

설치하지 않고 소스 폴더에서 `python3 -m channel_cli`로도 실행할 수 있다. 기본 출력은 터미널에서 읽는 표와 답변 본문이다. 채널 연동에는 `--json`을 붙인다. JSON 모드에서 명령의 stdout은 JSON이며, 상시 워커는 완료·실패 결과를 JSONL로 출력한다. 에이전트의 원본 출력은 stdout에 섞지 않는다. `--json`은 하위 명령 앞뒤 모두 지정할 수 있다. `channel agents --verbose`는 실행 파일 경로까지 표시한다. 작업은 화면에 표시한 8자리 ID로도 조회·취소·재시도할 수 있으며, 앞자리가 겹치면 더 긴 ID를 사용한다.


## 하나의 Atrium CLI

에이전트 실행 큐와 웹 대화 기능을 `atrium` 하나로 제공한다. 웹 동작은 이 패키지의 `channel_cli.web`에서 관리하며 Sentinel에는 별도 구현을 두지 않는다. 웹 명령에는 실행 중인 Atrium 웹 서버가 필요하다.

```sh
atrium send --agent codex --room my-test '프로젝트 구조를 설명해줘'
atrium jobs
atrium result <job-id>
atrium reply --agent pixie --topic dev --reply-to <message-id> --text '확인했어요'
atrium share ./result.png '작업 결과' --agent pixie --topic dev
atrium usage
```

`send`는 에이전트 작업을 큐에 넣고, `reply`는 웹에 답장을 게시한다. `--home`/`CHANNEL_HOME`은 큐 저장 위치를 정하며 웹 로그인·주소 설정과는 별개다. 웹 요청은 큐 DB를 만들지 않는다. 웹 인증은 `ATRIUM_TOKEN`, 없으면 기존 notify-token 파일을 읽고, 서버 주소는 `CORPUS_WEB_URL`을 사용한다. 기존 `ARTRIUM_TOKEN`·`ARTRIUM_PUBLIC_URL`도 호환된다. 토큰 값은 출력하지 않는다.

기존 `channel`, `atrium-cli`, `artrium-cli`는 모두 같은 CLI의 호환 명령이다. 업데이트할 때 이 패키지를 먼저 설치한 뒤 Sentinel을 반영한다. Sentinel의 `channel/atrium`·`channel/atrium-cli`도 이 설치를 호출하는 호환 진입점이며 `ATRIUM_CLI`로 실행 파일 위치를 지정할 수 있다. 새 설치에서는 PATH에 `atrium`이 있어야 하며 a1-2 기존 경로 `~/channel-cli/.venv/bin/atrium`도 탐색한다.

## 큐와 대화

- `claude`, `codex`, `agy`, `opencode`를 PATH에서 탐색한다. `@gemini`와 `--agent gemini`는 `agy`의 별칭이며 구 Gemini CLI를 실행하지 않는다. 설치 탐색은 인증·구독·모델 가용성을 검사하지 않는다. 실제 호출 오류는 해당 작업의 실패 상태와 로그로 확인한다.
- `@claude 질문` 또는 `--agent claude`로 에이전트를 지정한다. 표식이 없으면 `CHANNEL_DEFAULT_AGENT`를 사용하며, 기본값도 없으면 입력을 거부한다. 알 수 없는 멘션이나 서로 다른 에이전트의 복수 멘션은 거부한다. 이메일 안의 `@`는 표식으로 읽지 않는다.
- 큐 전체는 하나의 FIFO이며 워커 하나만 실행한다. 각 요청은 순차 처리된다. 에이전트별 병렬 처리는 첫 버전에 포함하지 않는다.
- 세션은 `(channel, room, agent)`별 네이티브 ID로 이어 간다. Codex는 방에서 아직 세션을 저장하지 않았을 때만 `CHANNEL_CODEX_THREAD`·`CHANNEL_CODEX_CWD` 또는 유일한 활성 본체 thread를 초기 세션으로 찾는다. 에이전트끼리 대화 맥락을 자동 공유하지 않는다.
- 방은 첫 메시지 때 작업 디렉터리에 연결된다. `--cwd /path/to/project`를 생략하면 상태 폴더 아래에 전용 디렉터리를 만든다. 기존 방의 디렉터리를 바꿀 때는 새 방을 사용한다.
- `--message-id`는 `(channel, room)` 범위의 중복 방지 키다. 같은 키와 본문·에이전트로 다시 넣으면 기존 작업을 돌려준다. 내용이 바뀌면 오류다. 키 없이 보내면 매번 새 작업이다.

```sh
export CHANNEL_DEFAULT_AGENT=agy
channel send --channel telegram --room topic-7 --message-id telegram-456 \
  --cwd /path/to/project '@codex 이 함수 설명해줘'
channel worker                 # 대기하며 계속 처리
channel worker --drain         # 현재 큐가 빌 때 종료
channel worker --once --timeout 120
channel send --agent codex-body --literal --fresh '새 주제로 시작해줘'
printf '%s' '@claude 여러 줄 메시지' | channel send --room notes -
```

## 떠 있는 Codex 세션에 넣기 (`codex-body`)

`@codex-body`는 새 Codex를 띄우지 않고 Codex app-server 데몬이 쥔 세션에 말을 넣는다. 그 세션을 `codex --remote unix://<소켓>`으로 연 TUI에는 넣은 말과 답이 실시간으로 보이고 기억도 하나로 이어진다. 같은 세션 ID로 `codex exec resume`을 걸면 Codex가 `already has an active writer`로 거부하므로 이 길을 쓴다.

- 소켓: `CHANNEL_CODEX_SOCKET`, 없으면 `$CODEX_HOME/app-server-control/app-server-control.sock`. `agents`는 소켓이 있으면 설치로 표시한다.
- 초기 세션 선택: `CHANNEL_CODEX_THREAD`, 다음으로 `CHANNEL_CODEX_CWD`(그 폴더의 가장 최근 세션), 없으면 데몬에 열린 세션 중 임시(ephemeral)가 아닌 것이 정확히 하나일 때 그것을 쓴다. 이후에는 방별 저장 ID로 `thread/resume`한다.
- 세션이 일하는 중(`active`)이면 끝날 때까지 기다렸다가 넣는다. 제한 시간 안에 끝나지 않으면 실패로 남긴다.
- `initialize` → `thread/resume` → `turn/start`로 넣고, 그 turn의 `agentMessage`를 `turn/completed`까지 모아 답으로 저장한다. 시작한 turn은 `turn` 이벤트로 알린다.
- `channel interrupt <작업>`: 실행 중인 작업의 turn을 `turn/interrupt`로 멈춘다. 작업은 `interrupted`가 된다.
- `channel steer <대기 작업>`: 같은 본체의 실행 중 작업에 대기 말을 추가한다. Codex는 실행 중 turn에 직접 얹고, Cora·Gemini는 현재 응답 뒤 같은 세션에서 후속 turn으로 처리한 다음 원 작업을 완료한다. 추가 지시 job은 `steered`가 되고, 원 작업의 최종 결과만 완료로 게시한다. 실행 중 작업이 없으면 오류를 내고 대기 상태를 보존한다.
- `channel send --fresh --agent codex-body|agy-body`: 이 요청부터 새 본체 대화로 시작한다. Codex는 app-server `thread/start`로 새 persistent thread를 만들고, Gemini는 본체 tmux 세션에서 `/clear`를 실행한다. 새 ID를 요청 완료 전에 저장하므로 실패 뒤 `retry`는 같은 새 대화에서 이어 간다. Cora 본체는 기존 MCP 세션을 유지하며 fresh 대상이 아니다.

## 채널 연결 계약

서버에서 대상을 이미 선택하고 본문에 인용·코드·다른 @표식이 포함되는 경우 `send --agent <이름> --literal`을 사용한다. 본문을 다시 멘션으로 해석하지 않는다.


1. 채널은 `send --json`에 채널 ID, 방 ID, 채널 메시지 ID와 본문을 전달한다. 반환되는 `id`가 작업 ID다.
2. 별도 프로세스에서 `worker --json`을 실행한다.
3. 채널은 `events --json --channel web --after <cursor>`를 호출한다. 이벤트는 `seq`, `job_id`, `type`, `created`, `payload`를 가진다. `payload`는 그 시점의 작업 전체이며 `channel`, `room`, `message_id`, `agent`, `state`, `answer`, `error`를 포함한다.
4. `done` 이벤트의 `answer`를 원래 방에 답장한다. `failed`·`interrupted`는 상태를 표시한다. 처리한 마지막 `seq`를 채널이 저장하고 다음 조회의 `--after`로 사용한다.

이벤트는 삭제하지 않고 저장한다. 전송과 커서 저장 사이에 채널이 꺼지면 재전송될 수 있으므로 채널도 `job_id + event.seq`로 중복을 처리해야 한다. 큐 워커가 외부 채널에 자동 게시하지는 않는다. 웹에 직접 게시할 때는 같은 CLI의 `reply` 등 웹 명령을 사용한다.

```sh
channel events --json --channel web --after 0 --limit 100
channel jobs --state failed
channel cancel <queued-job-id>
channel interrupt <running-body-job-id>
channel retry <failed-or-interrupted-job-id>
```

## 실패와 실행 권한

기본 제한 시간은 요청당 300초다. 시간 초과나 SIGINT/SIGTERM은 실행한 프로세스 그룹을 종료한다. 다음 워커가 시작되면 완료 기록 없이 남은 `running` 작업을 `interrupted`로 바꾼다. 실패·중단 작업은 자동 재시도하지 않는다. 명시적 재시도 전 원본 로그와 실행 결과를 확인해야 한다. 완료된 turn의 native session ID는 SQLite에 보존하므로 명시적으로 재시도하면 해당 세션에서 이어 간다. 실행 도중 종료된 turn은 자동으로 이어 하지 않는다. 에이전트가 작업을 수행한 직후 결과 저장 전에 종료되었을 수 있으므로 외부 부작용에 대한 exactly-once 실행은 보장하지 않는다. 실패 이후의 다른 대기 요청은 계속 처리된다.

SIGKILL·OS 장애는 종료 처리를 실행할 수 없으며 자식 에이전트가 남을 수 있다. 이 경우 잔존 프로세스를 확인한 뒤 워커를 재실행한다. `interrupt`는 실행 중인 live-body 작업만 대상으로 한다. 별도 CLI 프로세스로 실행한 에이전트는 작업별 중단 명령이 없어 worker 신호로 종료한다. `cancel`은 대기 작업에만 적용된다.

셸을 거치지 않고 실행하며 권한 우회 플래그는 사용하지 않는다. Claude·Codex·OpenCode는 프롬프트를 stdin으로 전달한다. `agy`는 현재 print 인터페이스가 요구하는 `--print=<본문>` 인자로 전달하므로 실행 중 로컬 프로세스 인자에 본문이 보일 수 있다. Claude는 `--strict-mcp-config`로 기존 MCP poller를 중복 시작하지 않는다. Codex 새 세션은 `read-only`, `agy`는 `plan`으로 시작한다. Claude·OpenCode는 각 도구의 기존 비대화형 권한 정책을 따른다. CLI 전체에 공통된 샌드박스를 제공하지 않는다. 에이전트 실행은 로컬 사용자의 인증과 설정을 사용한다.

## 저장 위치와 검사

기본 상태는 `~/.local/state/channel`에 저장한다. `CHANNEL_HOME` 또는 최상위 `--home`으로 분리할 수 있다. SQLite 큐·이벤트·세션과 `logs/<job-id>/<attempt>/stdout.log`, `stderr.log`를 저장한다. 메시지와 답변이 들어 있으므로 폴더·파일은 현재 사용자만 읽도록 생성한다. 자동 보존 기간·로그 정리는 아직 제공하지 않는다.

```sh
python3 -m unittest discover -s tests -v
```

검사는 임시 상태 폴더와 가짜 실행 파일로 큐·어댑터·세션·동시 접근·실패·종료 흐름을 실행한다. 실제 CLI의 인증과 출력 호환성은 각 환경에서 따로 확인한다.

## 세 본체의 공통 큐

`claude-body`(별칭 `cora-body`), `codex-body`, `agy-body`(별칭 `gemini-body`)는 실행 중인 본체에 요청하고 동일한 작업 완료 이벤트를 반환한다. Claude/Codex/agy를 새로 실행하는 기존 `claude/codex/agy`와 구분한다.

`claude-body`는 Atrium 웹 서버의 `/body-runs`를 통해 기존 Cora MCP 브리지에 전달한다. `ATRIUM_TOKEN` 또는 notify-token 파일로 인증하며, `CORPUS_WEB_URL`을 사용한다. 브리지 ACK가 없으면 전달 성공으로 처리하지 않는다. 최종 답은 `atrium reply --agent cora --run-id <id> --text '답'`로 제출하며, 워커가 공통 이벤트로 웹에 게시한다. `--run-id`는 reply에만 사용할 수 있다.

codex-body는 app-server turn steer를, claude-body·agy-body는 현재 응답 뒤 같은 본체 세션에서 후속 turn을 사용한다. `codex-body`·`agy-body`는 `--fresh` 새 대화를 지원하고, `claude-body`는 기존 Cora 세션을 유지한다. Cora body-run 상태는 웹 서버 상태 디렉터리에 저장하며, 서버 재시작 뒤 접수된 요청의 polling·최종 제출을 이어 가고 미전달 요청을 웹소켓 재연결 시 다시 전달한다. claim 후 알림 접수 확인 전 서버가 종료된 경계에서는 중복 전달 가능성이 있어 exactly-once 전달을 보장하지 않는다. 저장된 `cora` 세션 표시는 연결 정보일 뿐 새 CLI 세션 ID가 아니다. agy-body는 고유 요청 표식으로 해당 본체의 답만 수집한다.

하위 에이전트(clone)는 각 본체 런타임이 내부에서 만들고 관리한다. Atrium은 공통 큐 본체 작업의 시작·중단·추가 지시·최종 결과를 다루며, 본체 내부 clone의 생명주기·진행률·결과 합치기를 공통 API로 제공하지 않는다.
