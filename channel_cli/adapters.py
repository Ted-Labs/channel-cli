"""Native CLI transports. Execute argument lists without a shell."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

AGENTS = ("claude", "codex", "agy", "opencode", "codex-body", "agy-body", "claude-body")
# Agents reached through a daemon socket or tmux body instead of a new process per message.
BODIES = ("codex-body", "agy-body", "claude-body")
ALIASES = {"gemini": "agy", "gemini-body": "agy-body", "cora-body": "claude-body"}


class Interrupted(Exception):
    """The owned live-body request was stopped."""


@dataclass
class Reply:
    text: str
    session: str | None


def locate(agent):
    if agent == "claude-body":
        return shutil.which("claude")
    if agent == "codex-body":
        from .body import default_socket
        path = default_socket()
        return path if os.path.exists(path) else None
    if agent == "agy-body":
        from .agy_body import is_online, tmux_bin
        return tmux_bin() if is_online() else None
    return shutil.which(agent)


def discover():
    return [{"id": a, "aliases": [k for k, v in ALIASES.items() if v == a],
             "path": locate(a), "installed": bool(locate(a)),
             "authentication": "unchecked"} for a in AGENTS]


def command(agent, session, output, prompt=""):
    if agent == "claude":
        # Prevent a second Telegram MCP poller from interrupting the live channel.
        cmd = ["claude", "-p", "--output-format", "json", "--strict-mcp-config"]
        return cmd + (["--resume", session] if session else [])
    if agent == "codex":
        base = ["codex", "exec"]
        if session:
            base += ["resume", "--json", "--skip-git-repo-check", "-o", output, session, "-"]
        else:
            base += ["--json", "--skip-git-repo-check", "--sandbox", "read-only", "-o", output, "-"]
        return base
    if agent == "agy":
        return ["agy", "--output-format", "json", "--mode", "plan", "--print=" + prompt] + (
            ["--conversation", session] if session else [])
    if agent == "opencode":
        return ["opencode", "run", "--format", "json"] + (["--session", session] if session else [])
    raise ValueError(f"unsupported agent: {agent}")


def parse(agent, stdout, output=""):
    if agent in ("claude", "agy"):
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise ValueError("agent returned an unexpected JSON shape; inspect stdout.log")
        if data.get("is_error") or data.get("error") or data.get("status") == "ERROR":
            raise ValueError(str(data.get("error") or data.get("result") or data.get("errors")))
        text = data.get("result") if agent == "claude" else data.get("response")
        session = data.get("session_id") if agent == "claude" else data.get("conversation_id")
    else:
        text, session, parts = output, None, []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if agent == "codex":
                if event.get("type") == "thread.started":
                    session = event.get("thread_id")
                if event.get("type") in ("error", "turn.failed"):
                    raise ValueError(str(event.get("error") or event.get("message") or event))
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    raise ValueError("agent returned a malformed item; inspect stdout.log")
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    parts.append(item.get("text", ""))
            else:
                session = event.get("sessionID") or session
                if event.get("type") == "error":
                    raise ValueError(str(event.get("error") or event))
                if event.get("type") == "text":
                    part = event.get("part") or {}
                    if not isinstance(part, dict):
                        raise ValueError("agent returned a malformed part; inspect stdout.log")
                    parts.append(part.get("text", ""))
        if any(not isinstance(part, str) for part in parts):
            raise ValueError("agent returned a non-text message; inspect stdout.log")
        text = text or "\n".join(parts)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("agent returned no answer; raw output is in the job log")
    # Without an ID the next message would silently become a new conversation.
    if not isinstance(session, str) or not session:
        raise ValueError("agent returned no session ID; refusing to lose conversation continuity")
    return Reply(text.strip(), session)


def terminate(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            pass


def steer(agent, turn, text):
    """Add input to a live-body job using that runtime's native control path."""
    if agent == "codex-body":
        if not turn:
            raise ValueError("codex-body has no running turn; the job stays queued")
        from . import body
        body.steer(*turn, text)
        return "direct"
    if agent in ("claude-body", "agy-body"):
        # These interactive runtimes accept a follow-up turn in the same session.
        # The worker attaches it to the active queue job before publishing done.
        return "followup"
    raise ValueError(f"{agent} does not support steering")


def run(agent, prompt, cwd, session, timeout, logs, on_turn=None, on_session=None, fresh=False,
        on_part=None, should_interrupt=None):
    if agent == "claude-body":
        if fresh:
            raise ValueError("fresh sessions are not supported for claude-body")
        from . import claude_body
        return claude_body.run(prompt, timeout, **({"should_interrupt": should_interrupt} if should_interrupt else {}))
    if agent == "codex-body":
        # The live thread is shared by every room; the stored session is informational.
        from . import body
        return body.run(prompt, timeout, session=session, fresh=fresh, on_session=on_session,
                        on_turn=on_turn, on_part=on_part,
                        **({"should_interrupt": should_interrupt} if should_interrupt else {}))
    if agent == "agy-body":
        from . import agy_body
        return agy_body.run(prompt, timeout, session=session, fresh=fresh, on_session=on_session,
                            on_turn=on_turn, on_part=on_part,
                            **({"should_interrupt": should_interrupt} if should_interrupt else {}))
    executable = shutil.which(agent)
    if not executable:
        raise ValueError(f"agent executable not found: {agent}")
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="channel-reply-") as tmp:
        output = str(Path(tmp) / "reply.txt")
        cmd = command(agent, session, output, prompt)
        cmd[0] = executable
        with (logs / "stdout.log").open("w+") as out, (logs / "stderr.log").open("w+") as err:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=out,
                                    stderr=err, text=True, start_new_session=True)
            try:
                # agy print mode requires a prompt argument; other adapters read stdin.
                proc.communicate("" if agent == "agy" else prompt, timeout=timeout)
            except BaseException:
                terminate(proc)
                raise
            out.seek(0)
            raw = out.read()
            if proc.returncode:
                # Some CLIs emit provider errors as JSON on stdout, not stderr.
                detail = ""
                if raw.strip():
                    try:
                        parse(agent, raw)
                    except (ValueError, TypeError, AttributeError) as exc:
                        if not isinstance(exc, json.JSONDecodeError):
                            detail = str(exc)[:600]
                if not detail:
                    err.seek(0)
                    lines = err.read().splitlines()
                    detail = next((line for line in lines if line.lower().startswith(('error authenticating:', 'error:', 'fatal:'))), '')[:600]
                raise ValueError(f"{agent} exited with status {proc.returncode}"
                                 + (f": {detail}" if detail else "") + f"; logs: {logs}")
            answer = Path(output).read_text() if Path(output).exists() else ""
            return parse(agent, raw, answer)
