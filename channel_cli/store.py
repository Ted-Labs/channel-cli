"""SQLite queue. One worker lock serializes execution across all channels."""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
 channel TEXT NOT NULL, room TEXT NOT NULL, cwd TEXT NOT NULL,
 PRIMARY KEY(channel, room)
);
CREATE TABLE IF NOT EXISTS sessions (
 channel TEXT NOT NULL, room TEXT NOT NULL, agent TEXT NOT NULL, native_id TEXT NOT NULL,
 PRIMARY KEY(channel, room, agent)
);
CREATE TABLE IF NOT EXISTS jobs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 channel TEXT NOT NULL, room TEXT NOT NULL, agent TEXT NOT NULL,
 message_id TEXT, text TEXT NOT NULL, prompt TEXT NOT NULL, cwd TEXT NOT NULL,
 fresh INTEGER NOT NULL DEFAULT 0, fresh_started INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 90 CHECK(priority BETWEEN 0 AND 100),
 tier TEXT NOT NULL DEFAULT 'body' CHECK(tier IN ('body', 'worker')), assignment_reason TEXT, persona TEXT,
 answer TEXT, error TEXT,
 created REAL NOT NULL, started REAL, finished REAL, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(channel, room, message_id)
);
CREATE TABLE IF NOT EXISTS turns (
 job_id TEXT PRIMARY KEY, thread TEXT NOT NULL, turn TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interrupt_requests (
 job_id TEXT NOT NULL, attempt INTEGER NOT NULL, requested REAL NOT NULL,
 PRIMARY KEY(job_id, attempt)
);
CREATE TABLE IF NOT EXISTS steer_requests (
 source_job_id TEXT PRIMARY KEY, target_job_id TEXT NOT NULL,
 prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
 type TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cweb_restart (
 id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL, state TEXT NOT NULL,
 requested REAL NOT NULL, updated REAL NOT NULL, error TEXT
);
"""


class Store:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.home / "queue.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        # journal_mode can report SQLITE_BUSY immediately while another fresh
        # connection creates the database, even with sqlite's busy timeout.
        deadline = time.monotonic() + 15
        while True:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.executescript(SCHEMA)
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
                    if "fresh" not in columns:
                        self.db.execute("ALTER TABLE jobs ADD COLUMN fresh INTEGER NOT NULL DEFAULT 0")
                    if "fresh_started" not in columns:
                        self.db.execute("ALTER TABLE jobs ADD COLUMN fresh_started INTEGER NOT NULL DEFAULT 0")
                    if "priority" not in columns:
                        self.db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 90 CHECK(priority BETWEEN 0 AND 100)")
                    if "tier" not in columns:
                        self.db.execute("ALTER TABLE jobs ADD COLUMN tier TEXT NOT NULL DEFAULT 'body' CHECK(tier IN ('body', 'worker'))")
                        self.db.execute("UPDATE jobs SET tier='worker' WHERE agent NOT IN ('codex-body', 'agy-body', 'claude-body')")
                    if "assignment_reason" not in columns:
                        self.db.execute("ALTER TABLE jobs ADD COLUMN assignment_reason TEXT")
                    if "persona" not in columns:
                        self.db.execute("ALTER TABLE jobs ADD COLUMN persona TEXT")
                    self.db.execute("CREATE INDEX IF NOT EXISTS jobs_persona_queue ON jobs(state, persona, priority DESC, seq ASC)")
                    self.db.execute("CREATE INDEX IF NOT EXISTS jobs_queue_priority ON jobs(state, priority DESC, seq ASC)")
                    self.db.execute("CREATE INDEX IF NOT EXISTS jobs_agent_queue_priority ON jobs(state, agent, priority DESC, seq ASC)")
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
                break
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    self.db.close()
                    raise
                time.sleep(0.05)

    def close(self):
        self.db.close()

    def restart_paused(self):
        return bool(self.db.execute("SELECT 1 FROM cweb_restart WHERE id=1 AND state IN ('starting','waiting','restarting')").fetchone())

    def get(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None and re.fullmatch(r'[0-9a-f]{8,31}', job_id):
            matches = self.db.execute('SELECT * FROM jobs WHERE id LIKE ? LIMIT 2', (job_id + '%',)).fetchall()
            if len(matches) > 1:
                raise ValueError('ambiguous job ID; use a longer prefix')
            row = matches[0] if matches else None
        if row is None:
            raise ValueError(f"unknown job: {job_id}")
        job = dict(row)
        if job["state"] == "running" and self.interrupt_requested(job["id"], job["attempts"]):
            job["interrupt_requested"] = True
        return job

    def event(self, job_id, kind):
        job = self.get(job_id)
        self.db.execute("INSERT INTO events(job_id,type,created,payload) VALUES(?,?,?,?)",
                        (job_id, kind, time.time(), json.dumps(job, ensure_ascii=False)))

    def enqueue(self, channel, room, agent, text, prompt, cwd=None, message_id=None, fresh=False, priority=90, tier=None, assignment_reason=None, persona=None):
        if persona is not None and (not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", persona) or channel != f"atrium-web:{persona}"):
            raise ValueError("invalid persona or channel mismatch")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ValueError("priority must be an integer from 0 to 100")
        tier = tier or ("body" if agent in ("codex-body", "agy-body", "claude-body") else "worker")
        if tier not in ("body", "worker"):
            raise ValueError("invalid tier")
        with self.db:
            if fresh and agent not in ("codex-body", "agy-body"):
                raise ValueError("fresh sessions are supported for codex-body and agy-body only")
            # Take the write lock before checking room binding / idempotency.
            self.db.execute("BEGIN IMMEDIATE")
            if message_id:
                existing = self.db.execute("SELECT * FROM jobs WHERE channel=? AND room=? AND message_id=?",
                                           (channel, room, message_id)).fetchone()
                if existing:
                    if (existing["text"] != text or existing["agent"] != agent or
                            existing["fresh"] != int(fresh) or existing["priority"] != priority or existing["tier"] != tier or existing["assignment_reason"] != assignment_reason or existing["persona"] != persona):
                        raise ValueError("message-id already used with different content, agent, fresh flag, or priority")
                    return dict(existing)
            bound = self.db.execute("SELECT cwd FROM rooms WHERE channel=? AND room=?", (channel, room)).fetchone()
            if cwd:
                target = str(Path(cwd).expanduser().resolve())
                if not Path(target).is_dir():
                    raise ValueError(f"working directory does not exist: {target}")
                if bound and bound["cwd"] != target:
                    raise ValueError("room already bound to another working directory; use a new room")
            elif bound:
                target = bound["cwd"]
            else:
                digest = hashlib.sha256(json.dumps([channel, room]).encode()).hexdigest()[:24]
                workspace = self.home / "rooms" / digest
                workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
                target = str(workspace)
            self.db.execute("INSERT OR IGNORE INTO rooms VALUES(?,?,?)", (channel, room, target))
            job_id = uuid.uuid4().hex
            self.db.execute("""INSERT INTO jobs(id,channel,room,agent,message_id,text,prompt,cwd,fresh,priority,tier,assignment_reason,persona,created)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (job_id, channel, room, agent, message_id, text, prompt, target, int(fresh), priority, tier, assignment_reason, persona, time.time()))
            self.event(job_id, "queued")
        return self.get(job_id)

    def claim(self, agent=None, persona=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM cweb_restart WHERE id=1 AND state IN ('starting','waiting','restarting')").fetchone():
                return None
            where, params = ["j.state='queued'"], []
            if persona is not None:
                where.append("j.persona=?")
                params.append(persona)
            elif agent:
                where.append("j.persona IS NULL")
            if agent:
                where.append("j.agent=?")
                params.append(agent)
            # Legacy jobs can target the same live session as a persona job.
            where.append("""NOT EXISTS (SELECT 1 FROM jobs r WHERE r.state='running' AND
                ((j.persona IS NOT NULL AND (r.persona=j.persona OR (r.persona IS NULL AND r.channel=j.channel AND j.channel LIKE 'atrium-web:%'))) OR
                 (j.persona IS NULL AND (r.persona IS NOT NULL AND r.channel=j.channel AND j.channel LIKE 'atrium-web:%'))))""")
            row = self.db.execute("SELECT j.id FROM jobs j WHERE " + " AND ".join(where) +
                                  " ORDER BY j.priority DESC, j.seq ASC LIMIT 1", params).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET state='running',started=?,finished=NULL,answer=NULL,attempts=attempts+1 WHERE id=?",
                            (time.time(), row["id"]))
            self.db.execute("DELETE FROM turns WHERE job_id=?", (row["id"],))
            self.event(row["id"], "running")
        return self.get(row["id"])

    def session(self, job):
        row = self.db.execute("SELECT native_id FROM sessions WHERE channel=? AND room=? AND agent=?",
                              (job["channel"], job["room"], job["agent"])).fetchone()
        return row["native_id"] if row else None

    def set_session(self, job, session, consume_fresh=False):
        if not session:
            raise ValueError("native session ID is required")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                            (job["channel"], job["room"], job["agent"], session))
            if consume_fresh:
                self.db.execute("UPDATE jobs SET fresh_started=1 WHERE id=? AND state='running' AND attempts=?",
                                (job["id"], job["attempts"]))

    def finish(self, job, state, answer=None, error=None, session=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get(job["id"])
            if current["attempts"] != job["attempts"] or current["state"] not in ("queued", "running") or current["state"] == "queued" and job["attempts"] > 0:
                return current
            if state == "done" and self.interrupt_requested(job["id"], job["attempts"]):
                state, answer, error = "interrupted", None, "stopped by interrupt"
            if session:
                self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                                (job["channel"], job["room"], job["agent"], session))
            self.db.execute("UPDATE jobs SET state=?,answer=?,error=?,finished=? WHERE id=?",
                            (state, answer, error, time.time(), job["id"]))
            if state != "done":
                self._fail_steers(job["id"], f"target job ended as {state} before applying the instruction")
            self.event(job["id"], state)
        return self.get(job["id"])

    def _fail_steers(self, target_job_id, reason):
        rows = self.db.execute("SELECT source_job_id FROM steer_requests WHERE target_job_id=? AND state IN ('pending','claimed')",
                               (target_job_id,)).fetchall()
        for row in rows:
            self.db.execute("UPDATE steer_requests SET state='failed' WHERE source_job_id=?", (row["source_job_id"],))
            self.db.execute("UPDATE jobs SET state='failed',answer=NULL,error=?,finished=? WHERE id=? AND state='steered'",
                            (reason, time.time(), row["source_job_id"]))
            self.event(row["source_job_id"], "failed")

    def finish_or_steer(self, job, answer, session=None):
        """Atomically finish a job or claim an instruction added before completion."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get(job["id"])
            if current["attempts"] != job["attempts"] or current["state"] != "running":
                return {"job": current}
            if self.interrupt_requested(job["id"], job["attempts"]):
                self.db.execute("UPDATE jobs SET state='interrupted',answer=NULL,error='stopped by interrupt',finished=? WHERE id=?",
                                (time.time(), job["id"]))
                self._fail_steers(job["id"], "target job was interrupted before applying the instruction")
                self.event(job["id"], "interrupted")
                return {"job": self.get(job["id"])}
            row = self.db.execute("SELECT source_job_id,prompt FROM steer_requests WHERE target_job_id=? AND state='pending' ORDER BY created LIMIT 1",
                                  (job["id"],)).fetchone()
            if row:
                self.db.execute("UPDATE steer_requests SET state='claimed' WHERE source_job_id=?", (row["source_job_id"],))
                prior = self.db.execute("SELECT answer FROM jobs WHERE id=?", (job["id"],)).fetchone()["answer"] or ""
                if answer and not prior.endswith(answer):
                    accumulated = "\n".join(part for part in (prior, answer) if part)
                    self.db.execute("UPDATE jobs SET answer=? WHERE id=?", (accumulated, job["id"]))
                    self.event(job["id"], "part")
                return {"steer": {"id": row["source_job_id"], "prompt": row["prompt"]}}
            if session:
                self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                                (job["channel"], job["room"], job["agent"], session))
            self.db.execute("UPDATE jobs SET state='done',answer=?,error=NULL,finished=? WHERE id=?",
                            (answer, time.time(), job["id"]))
            result = self.get(job["id"])
            self.event(job["id"], "done")
            return {"job": result}

    def complete_steer(self, source_job_id):
        with self.db:
            self.db.execute("UPDATE steer_requests SET state='done' WHERE source_job_id=? AND state='claimed'",
                            (source_job_id,))

    def fail_steer(self, source_job_id, reason):
        with self.db:
            self.db.execute("UPDATE steer_requests SET state='failed' WHERE source_job_id=? AND state='claimed'",
                            (source_job_id,))
            self.db.execute("UPDATE jobs SET state='failed',answer=NULL,error=?,finished=? WHERE id=? AND state='steered'",
                            (reason, time.time(), source_job_id))
            self.event(source_job_id, "failed")

    def recover(self, agent=None, persona=None):
        # The caller owns the corresponding exclusive worker lock.
        where, params = ["state='running'"], []
        if persona is not None:
            where.append("persona=?")
            params.append(persona)
        elif agent:
            where.extend(["agent=?", "persona IS NULL"])
            params.append(agent)
        query = " AND ".join(where)
        with self.db:
            self.db.execute("UPDATE steer_requests SET state='pending' WHERE state='claimed' AND target_job_id IN (SELECT id FROM jobs WHERE " + query + ")", params)
        rows = self.db.execute("SELECT * FROM jobs WHERE " + query, params).fetchall()
        for row in rows:
            self.finish(dict(row), "interrupted", error="worker stopped before recording completion; inspect logs before retry")

    def change(self, job_id, action):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.get(job_id)
            job_id = job['id']
            if action == "cancel" and job["state"] == "queued":
                self.db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE id=?", (time.time(), job_id))
            elif action == "retry" and job["state"] in ("failed", "interrupted"):
                self.db.execute("UPDATE jobs SET state='queued',error=NULL,answer=NULL,started=NULL,finished=NULL WHERE id=?", (job_id,))
            else:
                raise ValueError(f"cannot {action} a {job['state']} job")
            self.event(job_id, action)
        return self.get(job_id)

    def interrupt_requested(self, job_id, attempt):
        return self.db.execute("SELECT 1 FROM interrupt_requests WHERE job_id=? AND attempt=?", (job_id, attempt)).fetchone() is not None

    def request_interrupt(self, job_id):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.get(job_id)
            if job["state"] != "running":
                raise ValueError(f"cannot interrupt a {job['state']} job")
            inserted = self.db.execute("INSERT OR IGNORE INTO interrupt_requests VALUES(?,?,?)", (job["id"], job["attempts"], time.time())).rowcount
            if inserted:
                self.event(job["id"], "interrupt_requested")
        return self.get(job["id"])

    def part(self, job_id, text):
        # 중간 답 하나가 끝날 때마다 남긴다. 채널은 'part' 이벤트를 새 말풍선으로 보인다.
        # answer 는 끝까지 모은 답이고, 끝날 때 finish 가 전체로 덮는다.
        with self.db:
            row = self.db.execute("SELECT answer, attempts FROM jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
            if row is None or self.interrupt_requested(job_id, row["attempts"]):
                return
            answer = "\n".join(p for p in (row["answer"], text) if p)
            self.db.execute("UPDATE jobs SET answer=? WHERE id=?", (answer, job_id))
            self.event(job_id, "part")

    def set_turn(self, job_id, thread, turn):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO turns VALUES(?,?,?)", (job_id, thread, turn))
            self.event(job_id, "turn")

    def turn(self, job_id):
        row = self.db.execute("SELECT thread, turn FROM turns WHERE job_id=?", (self.get(job_id)["id"],)).fetchone()
        return (row["thread"], row["turn"]) if row else None

    def running(self, agent, persona=None):
        row = self.db.execute("SELECT id FROM jobs WHERE state='running' AND agent=? AND persona IS ? ORDER BY seq DESC LIMIT 1",
                              (agent, persona)).fetchone()
        return self.get(row["id"]) if row else None

    def steered(self, job_id, into, defer_prompt=None):
        """Record an instruction as either native turn steering or a follow-up turn."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.get(job_id)
            if job["state"] != "queued":
                raise ValueError(f"cannot steer a {job['state']} job")
            target = self.get(into)
            if target["state"] != "running" or target["agent"] != job["agent"] or target["persona"] != job["persona"]:
                raise ValueError("target job is no longer running for this agent")
            if defer_prompt is not None:
                self.db.execute("INSERT INTO steer_requests(source_job_id,target_job_id,prompt,created) VALUES(?,?,?,?)",
                                (job["id"], target["id"], defer_prompt, time.time()))
            self.db.execute("UPDATE jobs SET state='steered',answer=?,finished=? WHERE id=?",
                            (f"steered into {target['id']}", time.time(), job["id"]))
            self.event(job["id"], "steered")
        return self.get(job["id"])

    def jobs(self, limit, state=None):
        if state:
            rows = self.db.execute("SELECT * FROM jobs WHERE state=? ORDER BY seq DESC LIMIT ?", (state, limit))
        else:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY seq DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    @staticmethod
    def _snapshot_job(row):
        # Keep this read model deliberately smaller than jobs/events: the web
        # canvas must not receive prompts, answers, cwd, or raw error output.
        raw_text = str(row["text"] or "")
        title = ""
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("[Atrium") or line.startswith("[atrium"):
                continue
            if line.startswith("[이 응답은") or line.startswith("[이 대화에서") or line.startswith("[응답 언어:"):
                continue
            if line.startswith("지금은 주인님(Ted)") or line.startswith("Ted와 직접") or line.startswith("코라가 전달한"):
                continue
            if line.startswith('{"chat_id"') or (line.startswith('{') and line.endswith('}') and '"chat_id"' in line):
                continue
            if line.startswith("[답장 대상]") or line.startswith("[첨부 경로]"):
                continue
            clean = re.sub(r"^(\[[^\]]*\]\s*)+", "", line).strip()
            clean = re.sub(r"^@[\w-]+\s*", "", clean).strip()
            if clean:
                title = clean[:96] + ("…" if len(clean) > 96 else "")
                break
        if not title:
            text = re.sub(r"\s+", " ", raw_text).strip()
            clean = re.sub(r"^(\[[^\]]*\]\s*)+", "", text).strip()
            clean = re.sub(r"^@[\w-]+\s*", "", clean).strip()
            title = clean[:96] + ("…" if len(clean) > 96 else "")
        return {
            "id": row["id"], "lane": row["lane"], "agent": row["agent"],
            "channel": row["channel"], "room": row["room"],
            "message_id": row["message_id"], "state": row["state"],
            "priority": row["priority"], "tier": row["tier"], "assignment_reason": row["assignment_reason"], "seq": row["seq"],
            "created": row["created"], "started": row["started"],
            "finished": row["finished"], "attempts": row["attempts"],
            "title": title or "웹 채팅 요청",
        }

    def snapshot(self, channel_prefix="", per_agent_limit=50, recent_limit=12, only_lane=None, offset=0):
        """Return bounded, prompt-free queue data grouped by channel lane."""
        columns = "id,channel,room,agent,message_id,state,priority,tier,assignment_reason,seq,created,started,finished,attempts,text"
        prefix = str(channel_prefix)
        where = "channel LIKE ? ESCAPE '\\'" if prefix else "1=1"
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = [escaped + "%"] if prefix else []
        if only_lane is not None:
            where += " AND channel=?"
            params.append(prefix + str(only_lane))
        counts = self.db.execute(
            f"SELECT channel, state, COUNT(*) AS count FROM jobs WHERE {where} GROUP BY channel, state",
            params,
        ).fetchall()
        lanes = {}
        for row in counts:
            lane = row["channel"][len(prefix):] if prefix else row["channel"]
            lanes.setdefault(lane, {"counts": {}})["counts"][row["state"]] = row["count"]

        active_rows = self.db.execute(
            f"SELECT {columns} FROM jobs WHERE {where} AND state='running' ORDER BY seq",
            params,
        ).fetchall()
        for row in active_rows:
            lane = row["channel"][len(prefix):] if prefix else row["channel"]
            lanes.setdefault(lane, {"counts": {}}).setdefault("running", []).append(
                self._snapshot_job({**dict(row), "lane": lane})
            )

        if only_lane is not None:
            queue_sql = f"SELECT {columns} FROM jobs WHERE {where} AND state='queued' ORDER BY priority DESC, seq ASC LIMIT ? OFFSET ?"
            queue_params = [*params, per_agent_limit, offset]
        else:
            queue_sql = f"SELECT {columns} FROM (SELECT {columns}, ROW_NUMBER() OVER (PARTITION BY channel ORDER BY priority DESC, seq ASC) AS lane_rank FROM jobs WHERE {where} AND state='queued') WHERE lane_rank<=? ORDER BY channel, priority DESC, seq ASC"
            queue_params = [*params, per_agent_limit]
        queued = self.db.execute(queue_sql, queue_params).fetchall()
        seen = {}
        for row in queued:
            lane = row["channel"][len(prefix):] if prefix else row["channel"]
            n = seen.get(lane, 0)
            seen[lane] = n + 1
            target = lanes.setdefault(lane, {"counts": {}})
            target.setdefault("queued", [])
            if only_lane is not None or n < per_agent_limit:
                target["queued"].append(self._snapshot_job({**dict(row), "lane": lane}))
        for lane, data in lanes.items():
            data.setdefault("running", [])
            data.setdefault("queued", [])
            data["queued_total"] = data["counts"].get("queued", 0)
            data["queued_offset"] = offset if only_lane == lane else 0
            data["queued_truncated"] = data["queued_total"] > (offset + len(data["queued"]) if only_lane == lane else len(data["queued"]))

        recent_rows = self.db.execute(
            f"SELECT {columns} FROM jobs WHERE {where} AND state IN ('done','failed','interrupted','cancelled','steered') ORDER BY seq DESC LIMIT ?",
            (*params, recent_limit),
        ).fetchall()
        recent = []
        for row in recent_rows:
            lane = row["channel"][len(prefix):] if prefix else row["channel"]
            recent.append(self._snapshot_job({**dict(row), "lane": lane}))
        cursor = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        totals = {}
        for data in lanes.values():
            for state, count in data["counts"].items():
                totals[state] = totals.get(state, 0) + count
        return {
            "generated_at": time.time(), "cursor": cursor,
            "totals": totals, "agents": lanes, "recent": recent,
        }

    def events(self, after, limit, channel=None):
        sql = "SELECT * FROM events WHERE seq>?"
        args = [after]
        if channel:
            sql += " AND json_extract(payload,'$.channel')=?"
            args.append(channel)
        rows = self.db.execute(sql + " ORDER BY seq LIMIT ?", (*args, limit))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
