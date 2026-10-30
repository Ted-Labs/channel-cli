"""Atrium web actions; the execution queue and web client share one CLI."""
import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

COMMANDS = ('reply', 'edit', 'react', 'buttons', 'relay', 'deliver', 'log', 'draft', 'share', 'handoff', 'usage')

def add_arguments(p):
    p.add_argument("subject", nargs="?")
    p.add_argument("body", nargs="?")
    p.add_argument("--thread", dest="relay_thread")
    p.add_argument("--agent", default=os.environ.get("CORPUS_PERSONA", ""))
    p.add_argument("--topic")
    p.add_argument("--reply-to")
    p.add_argument("--message-id")
    p.add_argument("--text")
    p.add_argument("--emoji")
    p.add_argument("--buttons", help='JSON, e.g. [[{"text":"확인","data":"ok"}]]')
    p.add_argument("--since", type=int)
    p.add_argument("--put", metavar="FILE")
    p.add_argument("--goal")
    p.add_argument("--roles", help='JSON, e.g. [{"agent":"gemini","task":"조사","state":"done"}]')
    p.add_argument("--checklist", help='JSON, e.g. [{"text":"톤 맞추기","done":false}]')
    p.add_argument("--merged", metavar="COMMIT")
    p.add_argument("--board", help="deliver: 이어 붙일 칠판 주제 id(첫 deliver 가 돌려준 board)")
    p.add_argument("--title", help="deliver: 칠판 주제 제목(Ted 부탁 한 줄 요약). 첫 deliver 에만")
    p.add_argument("--fresh", "--new-session", action="store_true", dest="fresh", help="deliver: 본체에 새 대화(세션)로 시작")


def register(sub):
    for name in COMMANDS:
        p = sub.add_parser(name, help=f"웹 채팅 {name}")
        add_arguments(p)
        p.set_defaults(action=name)
    for name in ("claude", "cora"):
        p = sub.add_parser(name, help="코라 모델·추론 강도 변경")
        p.add_argument("operation", choices=["change"])
        p.add_argument("model_args", nargs=argparse.REMAINDER)

def change(who, rest):
    import subprocess
    rest = list(rest)
    if who not in ("claude", "cora"): sys.exit(f"atrium: {who} change 는 아직 없다 (claude 만 된다)")
    efforts = ("low", "medium", "high", "xhigh", "max")
    effort = rest.pop() if rest and rest[-1] in efforts else None
    if not rest and not effort: sys.exit("usage: atrium claude change <opus|sonnet|haiku|fable> [5.5] [low|medium|high|xhigh|max]")
    cmds = []
    if rest:
        family, ver = rest[0].lower(), (rest[1] if len(rest) > 1 else "")
        cmds.append(f"/model claude-{family}-{ver.replace('.', '-')}" if ver else f"/model {family}")
    if effort: cmds.append(f"/effort {effort}")
    for c in cmds:
        r = subprocess.run(["tmux", "send-keys", "-t", "cora", c, "Enter"], capture_output=True, text=True)
        if r.returncode: sys.exit(f"atrium: tmux cora 에 못 넣었다: {r.stderr.strip()}")
        print(c)
    return


def run(a, p):
    token = (os.environ.get("ATRIUM_TOKEN") or os.environ.get("ARTRIUM_TOKEN"))
    if not token:
        try: token = (pathlib.Path.home()/".claude/channels/corpus-web/notify-token").read_text().strip()
        except OSError:
            if a.action != "usage": p.error("ATRIUM_TOKEN or notify-token is required")
            token = ""
    base = os.environ.get("CORPUS_WEB_URL", "http://127.0.0.1:8790")

    def request(path, method="GET", body=None, timeout=10):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base+path, data=data, headers={"authorization":"Bearer "+token, "content-type":"application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res: return json.loads(res.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"atrium: {exc}", file=sys.stderr); raise SystemExit(1)

    if a.action == "usage":
        if a.subject or a.body or a.relay_thread or a.topic or a.text or a.put or a.goal:
            p.error("usage does not accept extra positional or text options")
        cora = request("/sys/usage", timeout=15)
        agents = request("/sys/usage/agents", timeout=15)
        if a.json:
            print(json.dumps({"cora": cora, **agents}, ensure_ascii=False, indent=2))
            raise SystemExit()

        KST = datetime.timezone(datetime.timedelta(hours=9))
        def fmt_time(ts_str):
            if not ts_str: return "-"
            try:
                dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(KST)
                return dt.strftime("%m-%d %H:%M KST")
            except Exception:
                return ts_str[:16]

        print("=== 코라 (Claude) ===")
        if isinstance(cora, dict) and cora.get("session"):
            s = cora["session"]
            w = cora.get("weekly") or {}
            s_used = float(s.get("utilization", s.get("used_percent", 0)))
            w_used = float(w.get("utilization", w.get("used_percent", 0)))
            s_tag = " [한도 도달]" if s_used >= 100 else ""
            print(f"  5시간 : {s_used:>5.1f}%{s_tag} (리셋: {fmt_time(s.get('resets_at'))})")
            print(f"  주간  : {w_used:>5.1f}% (리셋: {fmt_time(w.get('resets_at'))})")
        else:
            print("  사용량 정보를 불러올 수 없습니다.")

        print("\n=== 픽시 (Codex) ===")
        pixie_list = agents.get("pixie", []) if isinstance(agents, dict) else []
        if pixie_list:
            for m in pixie_list:
                used = float(m.get("used", 0))
                tag = " [한도 도달]" if used >= 100 else ""
                print(f"  {m.get('name', '한도'):<6}: {used:>5.1f}%{tag} (리셋: {fmt_time(m.get('resetsAt'))})")
        else:
            print("  사용량 정보를 불러올 수 없습니다.")

        print("\n=== 제미나이 (AGY) ===")
        gemini_list = agents.get("gemini", []) if isinstance(agents, dict) else []
        if gemini_list:
            for m in gemini_list:
                used = float(m.get("used", 0))
                tag = " [한도 도달]" if used >= 100 else ""
                print(f"  {m.get('name', '한도'):<30}: {used:>5.1f}%{tag} (리셋: {fmt_time(m.get('resetsAt'))})")
        else:
            print("  사용량 정보를 불러올 수 없습니다.")
        raise SystemExit()

    if a.action == "handoff":
        if not a.subject or not a.merged: p.error("handoff requires <id> --merged <commit>")
        if a.body or a.relay_thread or a.topic or a.text: p.error("handoff does not accept text or topic options")
        print(json.dumps(request("/board/handoff/"+urllib.parse.quote(a.subject, safe=""), "POST", {"state":"merged", "commit":a.merged}), ensure_ascii=False))
        raise SystemExit()

    # share: Ted 에게 파일을 줄 때 쓴다(2026-10-27 우편함 M-20261027-07). 경로·내부 주소 대신 웹 채팅 첨부(/files)로 올리고,
    # 폰에서 열리는 도메인 주소로 직접 받아 200·형식을 확인한 뒤 그 링크만 내놓는다. 홈 밖(/data/jobs) 파일도 그대로 된다
    PUBLIC = (os.environ.get("ATRIUM_PUBLIC_URL") or os.environ.get("ARTRIUM_PUBLIC_URL") or "https://a1-2.tailacffe5.ts.net:8445")
    if a.action == "share":
        if not a.subject: p.error("share requires <file>")
        src = pathlib.Path(a.subject).expanduser().resolve()
        if not src.is_file(): p.error(f"no such file: {src}")
        who = a.agent if a.agent in ("pixie", "gemini") else "cora"
        res = request("/notify", "POST", {"topic": a.topic or (f"direct-{who}" if who in ("pixie", "gemini") else "dev"), "text": a.body or a.text or src.name, "file_path": str(src), "from": who})
        if not res.get("file"): print("atrium: 첨부가 만들어지지 않았다", file=sys.stderr); raise SystemExit(1)
        url = PUBLIC + res["file"]
        try:
            with urllib.request.urlopen(url, timeout=15) as r: code, kind, size = r.status, r.headers.get("content-type", ""), len(r.read())
        except Exception as exc:
            print(f"atrium: 도메인 링크 확인 실패 {url}: {exc}", file=sys.stderr); raise SystemExit(1)
        if code != 200 or size == 0:
            print(f"atrium: 링크 응답이 이상하다 {code} {kind} {size}B", file=sys.stderr); raise SystemExit(1)
        if not res["file"].endswith(".webp") and size != src.stat().st_size:
            print(f"atrium: 링크 응답 크기가 원본과 다르다 {code} {kind} {size}B (원본 {src.stat().st_size}B)", file=sys.stderr); raise SystemExit(1)
        print(json.dumps({"url": url, "type": kind, "bytes": size, "message": res.get("id")}, ensure_ascii=False))
        raise SystemExit()

    if a.action in ("log", "draft"):
        if not a.subject: p.error(f"{a.action} requires <thread>")
        if a.body or a.relay_thread: p.error(f"{a.action} does not accept extra positional text or --thread")
        path = "/threads/"+urllib.parse.quote(a.subject, safe="")
        if a.action == "log":
            if a.put or a.goal: p.error("log does not accept --put/--goal")
            result = request(path+"/log"+("" if a.since is None else "?since="+str(a.since)))
            if a.json: print(json.dumps(result, ensure_ascii=False)); raise SystemExit()
            for row in result:
                stamp = datetime.datetime.fromtimestamp(row["ts"]/1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                attachment = "" if not row.get("file") else " [첨부: "+row["file"]["name"]+"]"
                print(f'{row["seq"]} {stamp} {row["from"]}: {row["text"]}{attachment}')
            raise SystemExit()
        if a.since is not None: p.error("draft does not accept --since")
        if a.put:
            try: text = pathlib.Path(a.put).read_text()
            except OSError as exc: p.error(str(exc))
            if not a.agent: p.error("draft --put requires --agent or CORPUS_PERSONA")
            result = request(path+"/draft", "PUT", {"text": text, "by": a.agent, **({"goal": a.goal} if a.goal is not None else {}), **({"roles": json.loads(a.roles)} if a.roles else {}), **({"checklist": json.loads(a.checklist)} if a.checklist else {})})
        else:
            if a.goal: p.error("--goal requires --put")
            result = request(path+"/draft")
        if a.json: print(json.dumps(result, ensure_ascii=False)); raise SystemExit()
        current = result.get("versions", [])[-1] if result.get("versions") else None
        print("목표: "+result.get("goal", "(없음)"))
        print("판: "+(f'v{current["version"]} · {current["by"]}' if current else "(없음)"))
        if current: print(current["text"])
        raise SystemExit()

    if not a.agent: p.error("--agent or CORPUS_PERSONA is required")
    if a.action in ("relay", "deliver"):
        if not a.subject or not a.body: p.error(f"{a.action} requires <target> <text>")
        if a.text is not None: p.error(f"{a.action} text is positional")
        # deliver 는 코라만 쓴다(서버가 agent=cora 만 받는다): 픽시·Gemini 본체에 "[코라 중계] …" 로 넣는다
        body = {"action": a.action, "agent": a.agent, "target": a.subject, "text": a.body}
        if a.relay_thread is not None: body["thread"] = a.relay_thread
        if a.action == "deliver":
            if a.board: body["board"] = a.board
            if a.title: body["title"] = a.title
            if a.fresh: body["fresh"] = True
        elif a.board or a.title or a.fresh: p.error("--board/--title/--fresh are only for deliver")
        print(json.dumps(request("/agent-action", "POST", body), ensure_ascii=False))
        raise SystemExit()
    if a.subject or a.body or a.relay_thread or a.since is not None or a.put or a.goal: p.error("positional/thread options are only for relay/deliver/log/draft")
    body = {"action": a.action, "agent": a.agent}
    for src, dst in [(a.topic,"topic"),(a.reply_to,"replyTo"),(a.message_id,"messageId"),(a.text,"text"),(a.emoji,"emoji")]:
        if src is not None: body[dst] = src
    if a.buttons is not None: body["buttons"] = json.loads(a.buttons)
    print(json.dumps(request("/agent-action", "POST", body), ensure_ascii=False))
