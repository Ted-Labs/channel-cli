import argparse
import fcntl
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import adapters, body
from .store import Store
from .output import emit as display

MENTION = re.compile(r"(?<!\S)@([a-zA-Z][a-zA-Z0-9_-]*)(?=$|[\s,;:!?])")


def route(text, explicit=None, default=None):
    mentions = list(MENTION.finditer(text))
    targets = {adapters.ALIASES.get(m.group(1).lower(), m.group(1).lower()) for m in mentions}
    unknown = targets - set(adapters.AGENTS)
    if unknown:
        raise ValueError("unknown agent mention: " + ", ".join(sorted(unknown)))
    if len(targets) > 1:
        raise ValueError("one agent per message is supported; send separate messages")
    target = next(iter(targets), None)
    explicit = adapters.ALIASES.get(explicit, explicit)
    default = adapters.ALIASES.get(default, default)
    if explicit and target and explicit != target:
        raise ValueError("--agent conflicts with the message mention")
    agent = explicit or target or default
    if agent not in adapters.AGENTS:
        raise ValueError("specify @agent, --agent, or set CHANNEL_DEFAULT_AGENT")
    prompt = MENTION.sub("", text).strip()
    if not prompt:
        raise ValueError("message must contain text beyond the mention")
    return agent, prompt


def emit(value, args):
    display(value, args.command, args.json, getattr(args, 'verbose', False))


def positive(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return n


def bounded(value):
    n = int(value)
    if not 1 <= n <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return n


def parser():
    p = argparse.ArgumentParser(description="설치된 에이전트를 공통 메시지 큐로 호출합니다.")
    p.add_argument('--json', action='store_true', help='채널 연동용 JSON 출력')
    p.add_argument("--home", default=os.environ.get("CHANNEL_HOME", str(Path.home() / ".local/state/channel")))
    sub = p.add_subparsers(dest="command", required=True)
    agents = sub.add_parser("agents", help="설치된 에이전트 보기")
    agents.add_argument('--verbose', '-v', action='store_true', help='실행 파일 경로도 표시')
    send = sub.add_parser("send", help="enqueue a message; '-' reads stdin")
    send.add_argument("text")
    send.add_argument("--channel", default="cli")
    send.add_argument("--room", default="default")
    send.add_argument("--agent", choices=(*adapters.AGENTS, *adapters.ALIASES))
    send.add_argument('--literal', action='store_true', help='--agent를 사용하며 본문의 @표식을 해석하지 않음')
    send.add_argument("--cwd", help="bind the room to an existing directory on its first message")
    send.add_argument("--message-id", help="channel delivery key for idempotent enqueue")
    worker = sub.add_parser("worker", help="run the single global FIFO consumer; emits terminal jobs as JSONL")
    worker.add_argument("--once", action="store_true", help="process at most one job")
    worker.add_argument("--drain", action="store_true", help="exit when no jobs remain")
    worker.add_argument("--timeout", type=positive, default=300, help="seconds per agent call")
    worker.add_argument("--poll", type=positive, default=1, help="idle poll interval in seconds")
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--limit", type=bounded, default=20)
    jobs.add_argument("--state", choices=("queued", "running", "done", "failed", "interrupted", "cancelled", "steered"))
    stop = sub.add_parser("interrupt", help="stop the running turn of a daemon-backed job")
    stop.add_argument("job_id")
    steer = sub.add_parser("steer", help="add a queued job's text to the running turn of the same agent now")
    steer.add_argument("job_id")
    for name in ("result", "cancel", "retry"):
        item = sub.add_parser(name)
        item.add_argument("job_id")
    events = sub.add_parser("events", help="durable channel event feed; persist the last seq as a cursor")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=bounded, default=100)
    events.add_argument("--channel")
    for command_parser in sub.choices.values():
        command_parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                                    help='채널 연동용 JSON 출력')
    return p


def worker(store, args):
    # Child agents must not inherit the worker lock descriptor.
    with (store.home / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another worker is already running for this CHANNEL_HOME")
        store.recover()
        while True:
            job = store.claim()
            if job is None:
                if args.once or args.drain:
                    if args.once and not args.json:
                        print('대기 중인 작업이 없습니다.', flush=True)
                    return 0
                time.sleep(args.poll)
                continue
            try:
                if not args.json:
                    print(f"실행 중 · @{job['agent']} · {job['id'][:8]}\n", flush=True)
                reply = adapters.run(job["agent"], job["prompt"], job["cwd"], store.session(job),
                                     args.timeout, store.home / "logs" / job["id"] / str(job["attempts"]),
                                     on_turn=lambda thread, turn, j=job["id"]: store.set_turn(j, thread, turn))
                result = store.finish(job, "done", answer=reply.text, session=reply.session)
            except KeyboardInterrupt:
                emit(store.finish(job, "interrupted", error="worker interrupted; inspect logs before retry"), args)
                return 130
            except body.Interrupted:
                result = store.finish(job, "interrupted", error="stopped by interrupt")
            except subprocess.TimeoutExpired:
                result = store.finish(job, "failed", error=f"agent exceeded {args.timeout:g}s timeout; inspect logs before retry")
            except (OSError, ValueError) as exc:
                result = store.finish(job, "failed", error=str(exc))
            emit(result, args)
            if args.once:
                return 0 if result["state"] == "done" else 1


def main():
    # Restrict newly created database/WAL/log files to the current OS user.
    os.umask(0o077)
    args = parser().parse_args()
    store = None
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        if args.command == "agents":
            emit(adapters.discover(), args)
            return
        store = Store(args.home)
        if args.command == "send":
            text = sys.stdin.read() if args.text == "-" else args.text
            if not text.strip() or not args.channel.strip() or not args.room.strip():
                raise ValueError("message, channel and room must not be empty")
            if args.literal:
                if not args.agent:
                    raise ValueError('--literal requires --agent')
                agent, prompt = adapters.ALIASES.get(args.agent, args.agent), text.strip()
            else:
                agent, prompt = route(text, args.agent, os.environ.get("CHANNEL_DEFAULT_AGENT"))
            emit(store.enqueue(args.channel, args.room, agent, text, prompt, args.cwd, args.message_id), args)
        elif args.command == "worker":
            def interrupted(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupted)
            raise SystemExit(worker(store, args))
        elif args.command == "jobs":
            emit(store.jobs(args.limit, args.state), args)
        elif args.command == "result":
            emit(store.get(args.job_id), args)
        elif args.command == "interrupt":
            job = store.get(args.job_id)
            turn = store.turn(job["id"]) if job["state"] == "running" else None
            if not turn:
                raise ValueError(f"no running daemon turn for job {job['id'][:8]} ({job['state']})")
            body.interrupt(*turn)
            emit(store.get(job["id"]), args)
        elif args.command == "steer":
            job = store.get(args.job_id)
            if job["state"] != "queued":
                raise ValueError(f"cannot steer a {job['state']} job")
            if job["agent"] not in adapters.BODIES:
                raise ValueError(f"steer needs a daemon-backed agent, not {job['agent']}")
            current = store.running(job["agent"])
            turn = store.turn(current["id"]) if current else None
            if not turn:
                raise ValueError(f"@{job['agent']} has no running turn; the job stays queued")
            body.steer(*turn, job["prompt"])
            emit(store.steered(job["id"], current["id"]), args)
        elif args.command in ("cancel", "retry"):
            emit(store.change(args.job_id, args.command), args)
        elif args.command == "events":
            emit(store.events(args.after, args.limit, args.channel), args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        emit({"error": str(exc)}, args)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        if store:
            store.close()
