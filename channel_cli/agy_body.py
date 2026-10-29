"""Send a prompt into a live agy (Gemini) session running in tmux.

Pastes into the tmux session using bracketed paste and monitors the transcript
and tmux pane until the model completes its turn.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .adapters import Reply


class Interrupted(Exception):
    """The turn was stopped with interrupt."""


def default_session():
    return os.environ.get("CHANNEL_AGY_SESSION", "gemini")


def tmux_bin():
    return shutil.which("tmux") or "tmux"


def is_online(session=None):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "has-session", "-t", session],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def capture_pane(session=None, lines=8):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "capture-pane", "-pt", session, "-S", f"-{lines}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def is_busy(session=None):
    pane = capture_pane(session, lines=8)
    return bool(re.search(r"esc to (interrupt|cancel)|Press up to edit queued|Working\.\.\.|Running command|Loading\.\.\.", pane, re.I))


def latest_transcript():
    home = Path(os.environ.get("GEMINI_HOME") or Path.home())
    brains = sorted(glob.glob(str(home / ".gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl")),
                     key=os.path.getmtime)
    return Path(brains[-1]) if brains else None


def extract_latest_answer(transcript_path, min_line=0):
    if not transcript_path or not transcript_path.exists():
        return ""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for i in range(len(lines) - 1, min_line - 1, -1):
            try:
                row = json.loads(lines[i])
            except Exception:
                continue
            if row.get("source") == "MODEL" and row.get("type") == "PLANNER_RESPONSE":
                content = row.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    except Exception:
        pass
    return ""


def interrupt(session=None):
    session = session or default_session()
    subprocess.run([tmux_bin(), "send-keys", "-t", session, "Escape"], check=False)


def run(prompt, timeout=300, session=None, on_turn=None, on_part=None):
    session = session or default_session()
    if not is_online(session):
        raise ValueError(f"tmux session '{session}' is not online")

    deadline = time.monotonic() + timeout

    # Wait if session is busy
    while is_busy(session):
        if time.monotonic() > deadline:
            raise TimeoutError(f"session '{session}' remained busy for {timeout}s")
        time.sleep(0.5)

    transcript = latest_transcript()
    min_line = 0
    if transcript and transcript.exists():
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as f:
                min_line = sum(1 for _ in f)
        except Exception:
            min_line = 0

    # Paste prompt into tmux session
    buffer_name = f"channel-agy-{os.getpid()}"
    load_proc = subprocess.run([tmux_bin(), "load-buffer", "-b", buffer_name, "-"],
                               input=prompt.replace("\r\n", "\n").encode("utf-8"), check=False)
    if load_proc.returncode != 0:
        raise ValueError("failed to load prompt into tmux buffer")

    paste_proc = subprocess.run([tmux_bin(), "paste-buffer", "-p", "-b", buffer_name, "-t", session, "-d"], check=False)
    if paste_proc.returncode != 0:
        raise ValueError("failed to paste buffer into tmux session")

    time.sleep(0.4)
    send_proc = subprocess.run([tmux_bin(), "send-keys", "-t", session, "Enter"], check=False)
    if send_proc.returncode != 0:
        raise ValueError("failed to send Enter to tmux session")

    if on_turn:
        on_turn(session, f"turn-{int(time.time())}")

    # Wait for turn to start (becomes busy)
    started = False
    for _ in range(20):
        time.sleep(0.3)
        if is_busy(session):
            started = True
            break

    last_answer = ""
    # Monitor until completion
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"turn in '{session}' did not finish within {timeout}s")

        cur_answer = extract_latest_answer(transcript, min_line)
        if cur_answer and cur_answer != last_answer:
            last_answer = cur_answer
            if on_part:
                on_part(cur_answer)

        # Check if idle again after started
        if started and not is_busy(session):
            # Extra settling pause to make sure transcript flushed
            time.sleep(0.5)
            final_answer = extract_latest_answer(transcript, min_line) or last_answer
            if not final_answer:
                final_answer = "(작업 완료)"
            return Reply(final_answer, session)

        time.sleep(0.5)
