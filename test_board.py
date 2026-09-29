"""Focused regression tests. Run: python3 -m unittest -v test_board"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import server
from codex_source import CodexSource, Rollout


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.hooks = server.HookLog(str(self.path))

    def event(self, name, **fields):
        self.hooks._ingest(json.dumps(dict(session_id="parent", hook_event_name=name, ts=100, **fields)))

    def test_overlapping_tools_finish_by_id_and_failure_clears(self):
        self.event("PreToolUse", tool_name="Read", tool_use_id="a")
        self.event("PreToolUse", tool_name="Bash", tool_use_id="b")
        self.event("PostToolUse", tool_use_id="a")
        self.assertEqual(self.hooks.sessions["parent"]["tool"], "Bash")
        self.event("PostToolUseFailure", tool_use_id="b")
        self.assertIsNone(self.hooks.sessions["parent"]["tool"])

    def test_child_events_do_not_overwrite_parent(self):
        self.event("UserPromptSubmit", prompt="parent request")
        self.event("PreToolUse", tool_name="Agent", tool_use_id="spawn")
        self.event("SubagentStart", agent_id="child", agent_type="Explore")
        self.event("PreToolUse", agent_id="child", tool_name="Read", tool_use_id="read")
        self.event("SubagentStop", agent_id="child", last_assistant_message="finished")
        self.assertEqual(self.hooks.sessions["parent"]["tool"], "Agent")
        self.assertEqual(self.hooks.sessions["parent"]["prompt"], "parent request")
        self.assertEqual(self.hooks.children["parent"]["child"]["stop_ts"], 100)

    def test_torn_lines_and_rotation(self):
        event = json.dumps(dict(session_id="s", hook_event_name="Stop", ts=100)).encode()
        self.path.write_bytes(event[:20])
        self.hooks.poll()
        self.assertEqual(self.hooks.count, 0)
        with self.path.open("ab") as f:
            f.write(event[20:] + b"\n")
        self.hooks.poll()
        self.assertEqual(self.hooks.count, 1)
        replacement = self.path.with_suffix(".new")
        replacement.write_bytes(event + b"\n")
        replacement.replace(self.path)
        self.hooks.poll()
        self.assertEqual(self.hooks.count, 2)

    def test_malformed_events_are_ignored(self):
        for line in ("[]", "null", "bad", '{"session_id":"s","hook_event_name":"Stop","ts":"bad"}'):
            self.hooks._ingest(line)
        self.assertEqual(self.hooks.count, 0)


class BoardTests(unittest.TestCase):
    def test_child_attention_and_completion_expiry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "LOG_PATH", str(Path(tmp) / "events")), \
                patch.object(server, "read_tabs", return_value={}), patch.object(server, "missing_hooks", return_value=[]), \
                patch.object(server.CodexSource, "poll", return_value=[]), \
                patch.object(server, "read_agents", return_value=[dict(sessionId="s", status="busy", name="parent")]):
            board = server.Board()
            def emit(name, ts, **fields):
                board.hooks._ingest(json.dumps(dict(session_id="s", agent_id="a", hook_event_name=name, ts=ts, **fields)))
            emit("SubagentStart", 100, agent_type="Explore")
            emit("Notification", 101, notification_type="permission_prompt", message="Approval needed")
            with patch.object(server.time, "time", return_value=102):
                board.refresh()
            child = next(r for r in board.snapshot["sessions"] if r.get("agentId"))
            self.assertEqual(child["state"], "needs-you")
            self.assertEqual(child["detail"], "Approval needed")
            emit("SubagentStop", 110, last_assistant_message="done")
            with patch.object(server.time, "time", return_value=111):
                board.refresh()
            self.assertEqual(board.seen["s:agent:a"]["row"]["state"], "gone")
            with patch.object(server.time, "time", return_value=201):
                board.refresh()
            self.assertNotIn("s:agent:a", board.seen)

    def test_source_failure_preserves_rows_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "LOG_PATH", str(Path(tmp) / "events")), \
                patch.object(server, "read_tabs", return_value={}), patch.object(server, "hooks_installed", return_value=True), \
                patch.object(server, "missing_hooks", return_value=[]), patch.object(server.CodexSource, "poll", return_value=[]):
            board = server.Board()
            agent = dict(sessionId="s", status="busy", name="test")
            with patch.object(server, "read_agents", return_value=[agent]):
                board.refresh()
            with patch.object(server, "read_agents", side_effect=RuntimeError("offline")):
                board.refresh()
                row = json.loads(board.public_json)["sessions"][0]
                self.assertEqual(row["state"], "working")
                self.assertTrue(row["stale"])
            with patch.object(server, "read_agents", return_value=[agent]):
                board.refresh()
                self.assertFalse(json.loads(board.public_json)["sessions"][0]["stale"])
            with patch.object(server, "read_agents", return_value=[]):
                board.refresh()
                self.assertEqual(json.loads(board.public_json)["sessions"][0]["state"], "gone")

    def test_partial_hook_install_is_detected_and_preserves_other_hooks(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "SETTINGS_PATH", str(Path(tmp) / "settings.json")):
            settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}}
            Path(server.SETTINGS_PATH).write_text(json.dumps(settings))
            self.assertFalse(server.hooks_installed())
            server.install_hooks()
            self.assertTrue(server.hooks_installed())
            server.install_hooks()
            self.assertEqual(json.loads(Path(server.SETTINGS_PATH + ".session-board.bak").read_text()), settings)
            server.uninstall_hooks()
            self.assertEqual(server.load_settings(), settings)


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = sqlite3.connect(self.home / "state_5.sqlite")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE threads (id TEXT, rollout_path TEXT, created_at INTEGER,
                updated_at INTEGER, archived INTEGER, cwd TEXT, title TEXT, history_mode TEXT);
            CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
        """)

    def thread(self, sid, events, mode="legacy", updated=1000):
        path = self.home / (sid + ".jsonl")
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        self.db.execute("INSERT INTO threads VALUES (?, ?, 1, ?, 0, '/project', ?, ?)", (sid, str(path), updated, sid, mode))
        self.db.commit()
        return path

    def event(self, kind, **payload):
        return dict(timestamp="1970-01-01T00:16:40Z", type="event_msg", payload=dict(type=kind, **payload))

    def test_legacy_lifecycle_incremental_and_stale(self):
        path = self.thread("s", [self.event("task_started")])
        source = CodexSource(self.home)
        self.assertEqual(source.poll(1001)[0]["state"], "working")
        self.assertEqual(source.poll(1400)[0]["state"], "unknown")
        with path.open("a") as f:
            f.write(json.dumps(self.event("task_complete", last_agent_message="done")) + "\n")
        row = source.poll(1401)[0]
        self.assertEqual(row["state"], "resting")
        self.assertEqual(row["detail"], "done")

    def test_recorded_spawn_edge_includes_older_parent(self):
        self.thread("parent", [], updated=1)
        self.thread("child", [self.event("task_started")], updated=100000)
        self.db.execute("INSERT INTO thread_spawn_edges VALUES ('parent', 'child', 'running')")
        self.db.commit()
        rows = {r["key"]: r for r in CodexSource(self.home).poll(100001)}
        self.assertIn("codex:parent", rows)
        self.assertEqual(rows["codex:child"]["parentKey"], "codex:parent")

    def test_paginated_history_overrides_old_start(self):
        self.thread("s", [self.event("task_started")], mode="paginated")
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.executescript("""
                CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                    status TEXT, started_at INTEGER, completed_at INTEGER);
                CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                    item_json TEXT, created_at_ms INTEGER);
                INSERT INTO thread_turns VALUES ('s', 't', 1, 'completed', 1000, 1010);
            """)
            db.execute("INSERT INTO thread_items VALUES ('s', 't', 2, ?, 1010000)", (json.dumps(dict(type="agentMessage", text="complete")),))
        row = CodexSource(self.home).poll(1011)[0]
        self.assertEqual(row["state"], "resting")
        self.assertEqual(row["detail"], "complete")

    def test_missing_installation_and_incompatible_schema(self):
        self.assertEqual(CodexSource(self.home / "missing").poll(1000), [])
        self.db.execute("DROP TABLE threads")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            CodexSource(self.home).poll(1000)


if __name__ == "__main__":
    unittest.main()
