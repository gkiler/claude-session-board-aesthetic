"""Read-only Codex adapter for local thread databases and rollout files.

These are local implementation formats, not a stable API. Missing installations
are fine; incompatible schemas and unreadable files are reported by the board.
"""
import datetime
import json
import os
from pathlib import Path
import sqlite3


def stamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def clipped(value):
    return value[:500] if isinstance(value, str) else ""


def connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    db.row_factory = sqlite3.Row
    return db


class Rollout:
    def __init__(self):
        self.offset = 0
        self.inode = None
        self.state = "unknown"
        self.since = 0
        self.last = 0
        self.detail = ""
        self.tools = {}
        self.model = ""
        self.parent = None

    def poll(self, path):
        st = path.stat()
        if self.inode != st.st_ino or st.st_size < self.offset:
            self.__init__()
            self.inode = st.st_ino
            # Bound first read, even for very long sessions.
            self.offset = max(0, st.st_size - 1_000_000)
        with path.open("rb") as f:
            f.seek(self.offset)
            if self.offset and self.last == 0:
                f.readline()
                self.offset = f.tell()
            data = f.read(2_000_000)
        cut = data.rfind(b"\n") + 1
        self.offset += cut
        for line in data[:cut].splitlines():
            try:
                self.ingest(json.loads(line))
            except (ValueError, TypeError, AttributeError):
                continue

    def ingest(self, event):
        p = event.get("payload") or {}
        kind = event.get("type")
        ts = stamp(event.get("timestamp"))
        self.last = max(self.last, ts)
        t = p.get("type")
        if kind == "session_meta":
            source = p.get("source")
            if isinstance(source, dict):
                sub = source.get("subagent")
                spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
                if isinstance(spawn, dict):
                    self.parent = spawn.get("parent_thread_id")
        elif kind == "turn_context":
            self.model = p.get("model") or self.model
        elif kind == "event_msg":
            if t == "task_started":
                self.state, self.since = "working", ts
                self.tools.clear()
            elif t in ("task_complete", "turn_aborted"):
                self.state, self.since = "resting", ts
                self.tools.clear()
                self.detail = clipped(p.get("last_agent_message")) or ("interrupted" if t == "turn_aborted" else "")
        elif kind == "response_item":
            if t in ("function_call", "custom_tool_call"):
                self.tools[p.get("call_id")] = p.get("name") or "tool"
            elif t in ("function_call_output", "custom_tool_call_output"):
                self.tools.pop(p.get("call_id"), None)
            elif t == "message" and p.get("role") == "assistant":
                self.detail = clipped(" ".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict)))


class CodexSource:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.cache = {}

    def poll(self, now):
        databases = list(self.home.glob("state_*.sqlite"))
        if not databases:
            return []
        database = max(databases, key=lambda p: int(p.stem.split("_")[-1]))
        db = connect(database)
        history = None
        try:
            threads = [dict(r) for r in db.execute(
                "SELECT * FROM threads WHERE archived = 0 AND updated_at >= ? ORDER BY updated_at DESC LIMIT 100",
                (int(now - 86400),))]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            edges = {r["child_thread_id"]: r["parent_thread_id"] for r in db.execute("SELECT * FROM thread_spawn_edges")} if "thread_spawn_edges" in tables else {}
            # Include parents even when only the child has recent activity.
            by_id = {t["id"]: t for t in threads}
            pending = list(by_id)
            while pending:
                parent = edges.get(pending.pop())
                if parent and parent not in by_id:
                    r = db.execute("SELECT * FROM threads WHERE id = ?", (parent,)).fetchone()
                    if r:
                        by_id[parent] = dict(r)
                        pending.append(parent)
            histories = list(self.home.glob("thread_history_*.sqlite"))
            if histories:
                history = connect(max(histories, key=lambda p: int(p.stem.split("_")[-1])))
            rows = [self.row(t, edges, history, now) for t in by_id.values()]
            self.cache = {key: value for key, value in self.cache.items() if key in by_id}
            return rows
        finally:
            db.close()
            if history:
                history.close()

    def row(self, thread, edges, history, now):
        sid = thread["id"]
        digest = self.cache.setdefault(sid, Rollout())
        path = Path(thread["rollout_path"])
        digest.poll(path)
        state, since = digest.state, digest.since
        detail, last = digest.detail, digest.last
        tool = next(reversed(digest.tools.values()), "")
        # Recent Codex versions project activity into a separate SQLite database.
        if thread.get("history_mode") == "paginated":
            if history is None:
                raise RuntimeError("Codex paginated history database is missing")
            turn = history.execute("SELECT * FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal DESC LIMIT 1", (sid,)).fetchone()
            if turn:
                state = {"inProgress": "working", "completed": "resting", "interrupted": "resting", "failed": "blocked"}.get(turn["status"], "unknown")
                since = stamp(turn["completed_at"] or turn["started_at"])
                last = max(last, since)
                items = history.execute("SELECT item_json, created_at_ms FROM thread_items WHERE thread_id = ? AND turn_id = ? ORDER BY rollout_ordinal DESC LIMIT 40", (sid, turn["turn_id"])).fetchall()
                tool = ""
                latest_message = None
                for record in items:
                    item = json.loads(record["item_json"])
                    last = max(last, stamp(record["created_at_ms"]))
                    if item.get("type") == "agentMessage" and latest_message is None:
                        latest_message = clipped(item.get("text"))
                    if item.get("status") == "inProgress" and not tool:
                        tool = item.get("tool") or item.get("type") or "tool"
                if latest_message is not None:
                    detail = latest_message
                if turn["status"] == "failed":
                    detail = "turn failed"
        stale = state == "working" and now - last > 300
        if stale:
            state = "unknown"
        parent = edges.get(sid) or digest.parent
        name = thread.get("agent_nickname") or thread.get("agent_path") or thread.get("name") or "Codex · " + sid[:8]
        foot = {"working": tool or "working", "resting": "turn ended", "blocked": "turn failed", "unknown": "activity unconfirmed"}[state]
        return {"key": "codex:" + sid, "sessionId": sid, "provider": "codex",
            "parentKey": "codex:" + parent if parent else None,
            "kind": "subagent" if parent or thread.get("agent_role") else "interactive",
            "name": name, "title": clipped(thread.get("title")) or name,
            "cwd": thread["cwd"], "pid": None, "tty": None, "hasTab": False,
            "state": state, "state_since": since or thread["updated_at"], "stale": stale,
            "foot": foot, "detail": detail, "prompt": clipped(thread.get("first_user_message")),
            "model": thread.get("model") or digest.model, "lastActivity": last,
            "startedAt": thread["created_at"], "hooked": False, "toolsDone": None,
            "tracking": "local history · last 24h; status is last observed"}
