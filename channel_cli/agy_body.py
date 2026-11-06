"""Send a prompt into a live agy (Gemini) session running in tmux.

Pastes into the tmux session using bracketed paste and monitors the transcript
and tmux pane until the model completes its turn.
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .adapters import Reply, Interrupted


def default_session():
    return os.environ.get("CHANNEL_AGY_SESSION", "gemini")


def session_for_channel(channel):
    if not channel or not str(channel).startswith("atrium-web:"):
        return None
    persona = str(channel).split(":", 1)[1].strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", persona):
        return None
    if persona == "cora":
        return os.environ.get("CHANNEL_CORA_SESSION", "cora")
    if persona == "pixie":
        return os.environ.get("AGENT_PIXIE_TMUX", "pixie")
    if persona == "gemini":
        return os.environ.get("AGENT_GEMINI_TMUX") or default_session()
    env_key = f"AGENT_{persona.upper().replace('-', '_')}_TMUX"
    return os.environ.get(env_key, persona)


def tmux_bin():
    return shutil.which("tmux") or "tmux"


def is_online(session=None):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "has-session", "-t", session],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def is_agy_session(session):
    if not session or not is_online(session):
        return False
    proc = subprocess.run(
        [tmux_bin(), "list-panes", "-t", f"={session}", "-F", "#{pane_current_command} #{pane_start_command}"],
        capture_output=True, text=True,
    )
    out = getattr(proc, "stdout", "")
    if getattr(proc, "returncode", 1) == 0 and isinstance(out, str) and re.search(r"\bagy\b", out):
        return True
    return "Antigravity" in capture_pane(session, lines=30)


def resolve_session(session=None, channel=None):
    if not channel:
        return session or default_session()
    candidate = session_for_channel(channel)
    if candidate:
        if not is_agy_session(candidate):
            raise ValueError(f"Gemini session for {channel} is not online: {candidate}")
        return candidate
    if session and is_agy_session(session):
        return session
    return session or default_session()


def capture_pane(session=None, lines=8):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "capture-pane", "-pt", session, "-S", f"-{lines}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def is_busy(session=None):
    pane = capture_pane(session, lines=8)
    return bool(re.search(r"esc to (interrupt|cancel)|Press up to edit queued|Working\.\.\.|Running command|Loading\.\.\.", pane, re.I))


def get_full_content(full_path, line_index):
    try:
        with full_path.open("r", encoding="utf-8", errors="replace") as f:
            for j, line in enumerate(f):
                if j == line_index:
                    row = json.loads(line)
                    content = row.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                    break
    except Exception:
        pass
    return None


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
                if "content" in (row.get("truncated_fields") or []) or (isinstance(content, str) and "<truncated" in content):
                    full_path = transcript_path.with_name("transcript_full.jsonl")
                    full_content = get_full_content(full_path, i)
                    if full_content:
                        return full_content
                if isinstance(content, str) and content.strip():
                    return content.strip()
    except Exception:
        pass
    return ""


def system_generated_dir(transcript_path):
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path).resolve()
        if path.parent.name == "logs" and path.parent.parent.name == ".system_generated":
            return path.parent.parent
        for candidate in (
            path.parent.parent,
            path.parent,
            path.parent / ".system_generated",
            path.parent.parent / ".system_generated",
            path.parent.parent.parent / ".system_generated",
        ):
            if candidate.name == ".system_generated" and candidate.exists():
                return candidate
            if (candidate / ".system_generated").exists():
                return candidate / ".system_generated"
    except Exception:
        pass
    return None


def check_disk_task_completion(transcript_path, active_tasks, task_step_indices=None):
    if not transcript_path or not active_tasks:
        return
    try:
        gen_dir = system_generated_dir(transcript_path)
        if not gen_dir or not gen_dir.exists():
            return

        completed = set()
        task_step_indices = task_step_indices or {}
        for task_id in list(active_tasks):
            step_idx = task_step_indices.get(task_id)
            if step_idx is not None:
                step_file = gen_dir / "steps" / str(step_idx) / "output.txt"
                if step_file.exists():
                    try:
                        txt = step_file.read_text(encoding="utf-8", errors="replace")
                        if re.search(r"Command exited with code|finished with result|execution was canceled|Completed At:|status\s*[:=]\s*(done|success|error|failed|cancelled|canceled)", txt, re.I):
                            completed.add(task_id)
                            continue
                    except OSError:
                        pass

            log_file = gen_dir / "tasks" / f"{task_id}.log"
            if log_file.exists():
                try:
                    txt = log_file.read_text(encoding="utf-8", errors="replace")
                    end_txt = txt[-4096:] if len(txt) > 4096 else txt
                    if re.search(r"Command exited with code|finished with result|execution was canceled|Completed At:|Task .* finished", end_txt, re.I):
                        completed.add(task_id)
                        continue
                except OSError:
                    pass

            messages_dir = gen_dir / "messages"
            if messages_dir.exists():
                try:
                    for msg_file in messages_dir.glob("*.json"):
                        msg_txt = msg_file.read_text(encoding="utf-8", errors="replace")
                        if task_id in msg_txt and re.search(r"finished with result|was canceled with result|completed", msg_txt, re.I):
                            completed.add(task_id)
                            break
                except OSError:
                    pass

        active_tasks.difference_update(completed)
    except Exception:
        pass


def is_task_actively_running_on_disk(transcript_path, task_id, step_index=None):
    if not transcript_path:
        return False
    try:
        gen_dir = system_generated_dir(transcript_path)
        if not gen_dir or not gen_dir.exists():
            return False

        if step_index is not None:
            step_file = gen_dir / "steps" / str(step_index) / "output.txt"
            if step_file.exists():
                return False

        log_file = gen_dir / "tasks" / f"{task_id}.log"
        if log_file.exists():
            txt = log_file.read_text(encoding="utf-8", errors="replace")
            end_txt = txt[-4096:] if len(txt) > 4096 else txt
            if re.search(r"Command exited with code|finished with result|execution was canceled|Completed At:", end_txt, re.I):
                return False
            return True
    except Exception:
        pass
    return False


def transcript_has_unfinished_work(transcript_path, min_line=0, check_settled=False):
    """Whether this request has tool work that is still running or unanswered.

    Antigravity can emit a planner response before dispatching a command. That
    response is useful as progress, but it is not the turn result when a later
    tool step is still running or has not yet been followed by another planner
    response.
    """
    if not transcript_path or not transcript_path.exists():
        return False
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    latest_answer_line = -1
    latest_work_line = -1
    active_tasks = set()
    task_step_indices = {}
    task_id_pattern = re.compile(r"\btask-\d+\b", re.I)
    terminal_statuses = {"DONE", "SUCCESS", "ERROR", "FAILED", "CANCELLED", "CANCELED"}
    task_terminal_text = re.compile(
        r"\b(completed|complete|finished|exited|succeeded|failed|cancelled|canceled)\b", re.I
    )
    task_status_line = re.compile(
        r"^\s*(?:task\s+[^\r\n]+?\s+)?status\s*[:=]\s*"
        r"(running|pending|in progress|completed|complete|finished|exited|succeeded|failed|cancelled|canceled)\b",
        re.I | re.M,
    )
    preceding_tool_calls = []

    for index, line in enumerate(lines[min_line:], start=min_line):
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue

        row_type = str(row.get("type", "")).upper()
        if row.get("source") == "MODEL" and row_type == "PLANNER_RESPONSE":
            content = row.get("content")
            tool_calls = row.get("tool_calls")
            if isinstance(content, str) and content.strip() and not tool_calls:
                latest_answer_line = index
            elif tool_calls:
                # A planner response that dispatches a tool is progress even
                # when it also contains readable text.
                latest_work_line = index
            preceding_tool_calls = tool_calls if isinstance(tool_calls, list) else []
            continue

        if row_type in {"", "USER_INPUT", "SYSTEM"}:
            continue

        status = str(row.get("status", row.get("state", ""))).upper()
        content = row.get("content")
        text_content = content if isinstance(content, str) else ""
        task_ids = {task.lower() for task in task_id_pattern.findall(text_content)}
        explicit_id = row.get("task_id") or row.get("tool_call_id")
        if explicit_id is not None:
            explicit_task_ids = {task.lower() for task in task_id_pattern.findall(str(explicit_id))}
            task_ids.update(explicit_task_ids or {str(explicit_id).lower()})
        background_task = bool(re.search(
            r"running as a background task|background task with task id", text_content, re.I
        )) and row_type == "GENERIC" and status not in terminal_statuses

        managed_status_refs = set()
        managed_kill_refs = set()
        if row_type == "GENERIC":
            for call in preceding_tool_calls:
                if not isinstance(call, dict) or call.get("name") != "manage_task":
                    continue
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    continue
                action = str(args.get("Action", "")).strip('"').lower()
                refs = {task.lower() for task in task_id_pattern.findall(str(args.get("TaskId", "")))}
                if action == "status":
                    managed_status_refs.update(refs)
                elif action == "kill":
                    managed_kill_refs.update(refs)

        if status in {"RUNNING", "PENDING", "IN_PROGRESS"} or background_task:
            # `step_index` is a per-row sequence number, not a stable task ID.
            # Use the background task ID from the tool output; keep anonymous
            # work pending if the transcript did not provide one.
            new_ids = task_ids or {f"anonymous:{index}"}
            active_tasks.update(new_ids)
            step_idx = row.get("step_index")
            if step_idx is not None:
                for tid in new_ids:
                    task_step_indices[tid] = step_idx
            if row_type != "SYSTEM_MESSAGE":
                latest_work_line = index
        elif status in terminal_statuses:
            poll_refs = managed_status_refs & active_tasks
            poll_state = task_status_line.search(text_content) if poll_refs else None
            matched_active_task = bool(task_ids & active_tasks)
            if poll_refs:
                # The GENERIC row's DONE status only completes the status
                # query. The task's explicit Status field decides whether the
                # background command itself has ended.
                if poll_state and poll_state.group(1).lower() not in {"running", "pending", "in progress"}:
                    active_tasks.difference_update(poll_refs)
            elif managed_kill_refs & active_tasks and status in {"DONE", "SUCCESS"}:
                active_tasks.difference_update(managed_kill_refs)
            elif matched_active_task:
                if row_type == "SYSTEM_MESSAGE" or task_terminal_text.search(text_content):
                    active_tasks.difference_update(task_ids)
            elif row_type == "SYSTEM_MESSAGE" and not task_ids and re.search(
                r"\b(task|background task)\b.{0,120}\b(completed|complete|finished|exited|succeeded|failed|cancelled|canceled)\b",
                text_content, re.I | re.S,
            ) and len(active_tasks) == 1:
                # Some Antigravity completion notifications omit the task ID.
                active_tasks.clear()
            elif row_type != "SYSTEM_MESSAGE":
                # A tool result after the latest answer means the model still
                # needs a chance to incorporate it into its final response.
                latest_work_line = index

        if row_type == "GENERIC":
            preceding_tool_calls = []

    check_disk_task_completion(transcript_path, active_tasks, task_step_indices)

    if check_settled and active_tasks and latest_answer_line > latest_work_line:
        still_running = {
            tid for tid in active_tasks
            if is_task_actively_running_on_disk(transcript_path, tid, task_step_indices.get(tid))
        }
        active_tasks = still_running

    # A response before a tool step is progress. Wait for a newer planner
    # response after the step, unless a background task is still active.
    return latest_answer_line < 0 or bool(active_tasks) or latest_work_line > latest_answer_line


def request_transcript(marker):
    """Find the transcript containing this injection, never a merely recent session."""
    home = Path(os.environ.get("GEMINI_HOME") or Path.home())
    for path in home.glob(".gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            found = None
            for index, line in enumerate(lines):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "USER_INPUT":
                    if marker in str(row.get("content", "")):
                        found = index + 1
                    elif found is not None:
                        raise ValueError("Gemini received another input before this request completed")
            if found is not None:
                return path, found
        except OSError:
            continue
    return None


def interrupt(session=None):
    session = session or default_session()
    proc = subprocess.run([tmux_bin(), "send-keys", "-t", session, "Escape"], check=False)
    if proc.returncode:
        raise ValueError("failed to send interruption to Gemini")


def clear_session(session, on_session=None):
    proc = subprocess.run([tmux_bin(), "send-keys", "-t", session, "/clear", "Enter"], check=False)
    if proc.returncode:
        raise ValueError("failed to start a fresh Gemini conversation")
    time.sleep(0.4)
    if on_session:
        on_session(session)


def run(prompt, timeout=300, session=None, fresh=False, on_session=None,
        on_turn=None, on_part=None, should_interrupt=None, channel=None):
    session = resolve_session(session, channel)
    if not is_online(session):
        raise ValueError(f"tmux session '{session}' is not online")

    deadline = time.monotonic() + timeout

    # Wait if session is busy; cancellation here must not stop that other work.
    if should_interrupt and should_interrupt():
        raise Interrupted("stopped before delivering to Gemini")
    while is_busy(session):
        if should_interrupt and should_interrupt():
            raise Interrupted("stopped while waiting for Gemini")
        if time.monotonic() > deadline:
            raise TimeoutError(f"session '{session}' remained busy for {timeout}s")
        time.sleep(0.5)

    if fresh:
        clear_session(session, on_session)
    elif on_session:
        on_session(session)

    marker = '[Atrium request ' + str(uuid.uuid4()) + ']'
    prompt = marker + '\n' + prompt
    transcript = None
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

    stop_at = None
    unmatched_stop_at = None
    idle_hits = 0
    def stopping():
        nonlocal stop_at, unmatched_stop_at, idle_hits
        if not should_interrupt or not should_interrupt():
            return False
        # A unique marker proves the request belongs to this worker before Escape.
        if stop_at is None:
            if not request_transcript(marker):
                if not is_busy(session):
                    raise Interrupted("stopped while request delivery was unconfirmed")
                if unmatched_stop_at is None:
                    unmatched_stop_at = time.monotonic()
                elif time.monotonic() - unmatched_stop_at >= 15:
                    raise ValueError("cannot confirm request ownership; session was not interrupted")
                return True
            interrupt(session)
            stop_at = time.monotonic()
        if is_busy(session):
            idle_hits = 0
        else:
            idle_hits += 1
            if idle_hits >= 2 and time.monotonic() - stop_at >= 0.5:
                raise Interrupted("Gemini confirmed interruption")
        if time.monotonic() - stop_at > 15:
            raise ValueError("Gemini did not confirm interruption")
        return True

    # Wait for turn to start (becomes busy)
    started = False
    for _ in range(20):
        if stopping():
            time.sleep(0.1)
            continue
        time.sleep(0.3)
        if is_busy(session):
            started = True
            break

    last_answer = ""
    unconfirmed_idle_at = None
    # Monitor until completion
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"turn in '{session}' did not finish within {timeout}s")

        if stopping():
            time.sleep(0.1)
            continue
        match = request_transcript(marker)
        if match:
            transcript, min_line = match
            unconfirmed_idle_at = None
        elif not is_busy(session):
            if unconfirmed_idle_at is None:
                unconfirmed_idle_at = time.monotonic()
            elif time.monotonic() - unconfirmed_idle_at >= 30:
                raise ValueError("Gemini request delivery was not confirmed; inspect session before retry")
        else:
            unconfirmed_idle_at = None
        cur_answer = extract_latest_answer(transcript, min_line) if transcript else ""
        if cur_answer and cur_answer != last_answer:
            last_answer = cur_answer
            if on_part:
                on_part(cur_answer)

        # Check if idle again after started
        if (started or last_answer) and not is_busy(session):
            # Extra settling pause to make sure transcript flushed
            time.sleep(0.5)
            final_answer = extract_latest_answer(transcript, min_line) or last_answer
            if not final_answer or is_busy(session):
                continue
            if transcript_has_unfinished_work(transcript, min_line, check_settled=True):
                continue
            return Reply(final_answer, session)

        time.sleep(0.5)
