#!/usr/bin/env python3
"""Session Board: a local dashboard for many concurrent Claude Code windows.

Stdlib only. Every POLL seconds it merges three sources into one snapshot:

  1. `claude agents --json`      every session on this Mac (name, cwd, pid, status)
  2. Terminal.app via AppleScript tab title + tty for every tab, so a session
                                 can be matched (pid -> tty -> tab) and raised
  3. the hook event log          ~/.claude/session-board/events.jsonl, written
                                 by hook.sh (install with --install-hooks)

The snapshot streams to the page over Server-Sent Events. Clicking a lantern
POSTs /api/focus, which selects that Terminal tab and brings it to the front.

Usage:
  python3 server.py                 serve on http://127.0.0.1:7777
  python3 server.py --port 8000
  python3 server.py --install-hooks add the hook.sh entries to ~/.claude/settings.json
  python3 server.py --uninstall-hooks
  python3 server.py --once          print one snapshot as JSON and exit
"""
import argparse
import errno
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
HOOK_CMD = os.path.join(HERE, "hook.sh")
LOG_PATH = os.environ.get("SESSION_BOARD_LOG") or os.path.expanduser("~/.claude/session-board/events.jsonl")
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")

POLL = 2.0
GONE_TTL = 90.0          # seconds a vanished session lingers as "gone"
NOTE_TTL = 15 * 60       # a permission/question notification older than this no longer forces needs-you
NEEDS_EVENTS = ("permission_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog")
HOOK_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification"]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- sources

def read_agents():
    """Rows from `claude agents --json`. Empty list if the CLI is missing or fails."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not on PATH")
    out = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(f"claude agents failed: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout or "[]")
    return data if isinstance(data, list) else []


def tty_for_pids(pids):
    """pid -> 'ttys002' via one ps call."""
    if not pids:
        return {}
    out = subprocess.run(["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)],
                         capture_output=True, text=True, timeout=5)
    result = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] != "??":
            result[int(parts[0])] = parts[1]
    return result


TABS_SCRIPT = '''
set d to ASCII character 9
tell application "Terminal"
  set out to ""
  repeat with w in windows
    repeat with t in tabs of w
      set out to out & (id of w) & d & (tty of t) & d & (busy of t) & d & (custom title of t) & linefeed
    end repeat
  end repeat
  return out
end tell
'''


def read_tabs():
    """tty ('ttys002') -> {window, busy, title}. Empty dict if Terminal is not running."""
    out = subprocess.run(["osascript", "-e", TABS_SCRIPT], capture_output=True, text=True, timeout=8)
    tabs = {}
    if out.returncode != 0:
        raise RuntimeError(f"osascript failed: {out.stderr.strip()[:200]}")
    for line in out.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        win, tty, busy, title = parts
        tabs[tty.replace("/dev/", "")] = {"window": win, "busy": busy == "true", "title": title}
    return tabs


FOCUS_SCRIPT = '''
on run argv
  set wanted to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if (tty of t) is wanted then
          set selected of t to true
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return "not found"
end run
'''


def focus_tty(tty):
    out = subprocess.run(["osascript", "-e", FOCUS_SCRIPT, "/dev/" + tty], capture_output=True, text=True, timeout=8)
    return out.stdout.strip() == "ok"


# ---------------------------------------------------------------- hook log

class HookLog:
    """Tails events.jsonl and keeps a per-session digest of what matters."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.inode = None
        self.sessions = {}      # session_id -> digest
        self.count = 0
        self.last_ts = None
        self._load_initial()

    def _load_initial(self):
        if not os.path.exists(self.path):
            return
        size = os.path.getsize(self.path)
        # Only replay the tail of a big log; older history is not shown anyway.
        start = max(0, size - 2_000_000)
        self.inode = os.stat(self.path).st_ino
        with open(self.path, "rb") as f:
            f.seek(start)
            if start:
                f.readline()
            self.offset = f.tell()
            self._read_complete_lines(f)

    def _read_complete_lines(self, f):
        """Ingest only whole lines; a torn tail (another session mid-write) waits for the next poll."""
        data = f.read()
        cut = data.rfind(b"\n") + 1
        for line in data[:cut].splitlines():
            self._ingest(line)
        self.offset += cut

    def poll(self):
        if not os.path.exists(self.path):
            return
        st = os.stat(self.path)
        if st.st_ino != self.inode or st.st_size < self.offset:   # rotated or truncated
            self.inode = st.st_ino
            self.offset = 0
        if st.st_size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            self._read_complete_lines(f)

    def _ingest(self, line):
        try:
            ev = json.loads(line)
        except Exception:
            return
        sid = ev.get("session_id")
        name = ev.get("hook_event_name")
        ts = ev.get("ts")
        if not sid or not name or ts is None:
            return
        self.count += 1
        self.last_ts = ts
        d = self.sessions.setdefault(sid, {
            "prompt": None, "prompt_ts": None, "tool": None, "tool_ts": None, "tool_detail": None,
            "stop_ts": None, "start_ts": None, "end_ts": None, "note": None, "note_type": None,
            "note_ts": None, "tools_done": 0, "last_ts": None, "last_event": None, "cwd": None,
            "last_assistant_message": None,
        })
        d["last_ts"] = ts
        d["last_event"] = name
        if ev.get("cwd"):
            d["cwd"] = ev["cwd"]
        if name == "SessionStart":
            d["start_ts"] = ts
            d["end_ts"] = None
        elif name == "SessionEnd":
            d["end_ts"] = ts
        elif name == "UserPromptSubmit":
            d["prompt"] = ev.get("user_prompt")
            d["prompt_ts"] = ts
            d["stop_ts"] = None
            d["note"] = d["note_type"] = d["note_ts"] = None
            d["tool"] = None
        elif name == "PreToolUse":
            d["tool"] = ev.get("tool_name")
            d["tool_ts"] = ts
            ti = ev.get("tool_input") or {}
            d["tool_detail"] = ti.get("description") or ti.get("command") or ti.get("file_path") or ti.get("pattern") or ti.get("skill") or ti.get("url")
            d["note"] = d["note_type"] = d["note_ts"] = None
        elif name == "PostToolUse":
            d["tool"] = None
            d["tools_done"] += 1
            d["note"] = d["note_type"] = d["note_ts"] = None
        elif name == "Stop":
            d["stop_ts"] = ts
            d["last_assistant_message"] = ev.get("last_assistant_message")
            d["tool"] = None
            d["note"] = d["note_type"] = d["note_ts"] = None
        elif name == "Notification":
            d["note_type"] = ev.get("notification_type")
            d["note"] = ev.get("message")
            d["note_ts"] = ts


# ---------------------------------------------------------------- merge

def short_cwd(cwd):
    home = os.path.expanduser("~")
    if cwd and cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    return cwd or ""


def clean_title(title):
    if not title:
        return ""
    t = title.strip()
    for glyph in ("✳", "◐", "◑", "◒", "◓", "●", "○"):
        if t.startswith(glyph):
            t = t[len(glyph):].strip()
    return t


class Board:
    def __init__(self):
        self.hooks = HookLog(LOG_PATH)
        self.seen = {}         # key -> {"row": merged row, "gone_at": ts or None, "state_since": ts, "state": ...}
        self.snapshot = {"sessions": [], "meta": {}}
        self.errors = {}
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.public_json = json.dumps({"sessions": [], "meta": {}})
        self.version = 0
        self.cond = threading.Condition()

    def refresh(self):
        with self.refresh_lock:
            return self._refresh()

    def _refresh(self):
        now = time.time()
        agents, tabs, ttys = [], {}, {}
        try:
            agents = read_agents()
            self.errors.pop("agents", None)
        except Exception as e:
            self.errors["agents"] = str(e)
        try:
            tabs = read_tabs()
            self.errors.pop("terminal", None)
        except Exception as e:
            self.errors["terminal"] = str(e)
        try:
            self.hooks.poll()
            self.errors.pop("hooks", None)
        except Exception as e:
            self.errors["hooks"] = str(e)

        pids = [a["pid"] for a in agents if a.get("pid")]
        try:
            ttys = tty_for_pids(pids) if pids else {}
            self.errors.pop("ps", None)
        except Exception as e:
            self.errors["ps"] = str(e)

        live_keys = set()
        for a in agents:
            key = a.get("sessionId") or a.get("id")
            if not key:
                continue
            live_keys.add(key)
            entry = self.seen.get(key)
            row = self.merge_row(a, ttys, tabs, now, entry)
            if entry is None:
                entry = {"state": row["state"], "state_since": now, "gone_at": None}
                self.seen[key] = entry
            if entry["gone_at"] is not None:
                entry["gone_at"] = None
                entry["state_since"] = now
            if entry["state"] != row["state"]:
                entry["state"] = row["state"]
                entry["state_since"] = now
            row["state_since"] = self.state_since(row, entry)
            if row["state"] == "resting" and not (row["_hook"] or {}).get("stop_ts"):
                row["foot"] = f"resting {fmt_dur(now - entry['state_since'])}"
            entry["row"] = row

        # Sessions that vanished linger briefly as "gone".
        for key, entry in list(self.seen.items()):
            if key in live_keys:
                continue
            if entry["gone_at"] is None:
                entry["gone_at"] = now
                entry["row"]["state"] = "gone"
                entry["row"]["state_since"] = now
                entry["row"]["foot"] = "gone"
            elif now - entry["gone_at"] > GONE_TTL:
                del self.seen[key]

        rows = [e["row"] for e in self.seen.values()]
        rows.sort(key=lambda r: (r.get("startedAt") or 0, r["name"]))
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        snap = {
            "sessions": rows,
            "meta": {
                "now": now,
                "poll": POLL,
                "counts": counts,
                "hooks_installed": hooks_installed(),
                "hook_events": self.hooks.count,
                "hook_last_ts": self.hooks.last_ts,
                "log_path": LOG_PATH,
                "errors": dict(self.errors),
            },
        }
        public = json.dumps({
            "sessions": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
            "meta": snap["meta"],
        })
        with self.lock:
            changed = json.dumps(snap["sessions"], sort_keys=True, default=str) != json.dumps(self.snapshot["sessions"], sort_keys=True, default=str)
            self.snapshot = snap
            self.public_json = public
        with self.cond:
            self.version += 1
            self.cond.notify_all()
        return changed

    def state_since(self, row, entry):
        """Prefer the hook timestamp that started the current state; else our own observation."""
        h = row.get("_hook") or {}
        st = row["state"]
        if st == "working" and h.get("prompt_ts"):
            return h["prompt_ts"]
        if st == "resting" and h.get("stop_ts"):
            return h["stop_ts"]
        if st == "needs-you" and h.get("note_ts"):
            return h["note_ts"]
        return entry["state_since"]

    def merge_row(self, a, ttys, tabs, now, entry=None):
        sid = a.get("sessionId") or a.get("id")
        pid = a.get("pid")
        tty = ttys.get(pid) if pid else None
        tab = tabs.get(tty) if tty else None
        h = self.hooks.sessions.get(sid)
        kind = a.get("kind", "interactive")
        started = a.get("startedAt")
        started_s = started / 1000.0 if isinstance(started, (int, float)) and started > 1e11 else started

        # --- state
        if kind == "background":
            bg = (a.get("state") or "").lower()
            if bg == "blocked":
                state = "blocked"
            elif bg in ("running", "busy", "working", "active"):
                state = "working"
            elif bg in ("done", "completed", "finished", "exited", "stopped"):
                state = "gone"
            else:
                state = "resting"
        else:
            status = (a.get("status") or "").lower()
            if status == "waiting":
                state = "needs-you"
            elif status == "busy":
                state = "working"
            else:
                state = "resting"
            if h and h.get("note_type") in NEEDS_EVENTS and h.get("note_ts") and now - h["note_ts"] < NOTE_TTL:
                # A permission or input prompt that nothing has answered yet.
                state = "needs-you"

        # --- name / title
        name = a.get("name") or (sid[:8] if sid else "?")
        if kind == "background":
            name = f"{name} · bg"
        title = clean_title(tab["title"]) if tab else ""
        if (not title or title == "Terminal") and h and h.get("prompt"):
            title = h["prompt"]
        if not title:
            title = short_cwd(a.get("cwd")) or "untitled"

        # --- foot line
        foot, detail = "", ""
        if state == "working":
            if h and h.get("tool"):
                foot = f"{h['tool']} · {fmt_dur(now - h['tool_ts'])}"
                detail = h.get("tool_detail") or ""
            elif h and h.get("prompt_ts"):
                foot = f"thinking · {fmt_dur(now - h['prompt_ts'])}"
            else:
                foot = "working"
        elif state == "needs-you":
            if h and h.get("note_type") in NEEDS_EVENTS:
                label = {"permission_prompt": "permission", "agent_needs_input": "question"}.get(h["note_type"], "input")
                what = h.get("tool") or ""
                foot = f"{label}{': ' + what if what else ''} · {fmt_dur(now - h['note_ts'])}"
                detail = h.get("note") or ""
            elif h and h.get("tool"):
                foot = f"permission: {h['tool']}"
                detail = h.get("tool_detail") or ""
            else:
                foot = "needs you"
        elif state == "resting":
            since = (h.get("stop_ts") if h else None) or (entry or {}).get("state_since") or now
            foot = f"resting {fmt_dur(now - since)}"
            detail = (h.get("last_assistant_message") if h else "") or ""
        elif state == "blocked":
            foot = "blocked"
        else:
            foot = "gone"

        return {
            "key": sid,
            "sessionId": sid,
            "pid": pid,
            "kind": kind,
            "name": name,
            "title": title,
            "cwd": short_cwd(a.get("cwd")),
            "tty": tty,
            "hasTab": tab is not None,
            "state": state,
            "foot": foot,
            "detail": detail,
            "prompt": (h.get("prompt") if h else None),
            "toolsDone": (h.get("tools_done") if h else 0),
            "startedAt": started_s,
            "hooked": h is not None,
            "_hook": h,
        }


def fmt_dur(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s" if m < 10 else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ---------------------------------------------------------------- hooks install

def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        return {}
    with open(SETTINGS_PATH) as f:
        return json.load(f)


def hooks_installed():
    try:
        hooks = load_settings().get("hooks", {})
    except Exception:
        return False
    return any(
        HOOK_CMD in (h.get("command") or "")
        for groups in hooks.values() for g in groups for h in g.get("hooks", [])
    )


def write_settings(settings):
    """Back up settings.json, then replace it atomically."""
    backup = SETTINGS_PATH + ".session-board.bak"
    if os.path.exists(SETTINGS_PATH):
        shutil.copy2(SETTINGS_PATH, backup)
    tmp = SETTINGS_PATH + ".session-board.tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS_PATH)
    return backup


def install_hooks():
    settings = load_settings()
    hooks = settings.setdefault("hooks", {})
    added = 0
    for ev in HOOK_EVENTS:
        groups = hooks.setdefault(ev, [])
        if any(HOOK_CMD in (h.get("command") or "") for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]})
        added += 1
    if added:
        backup = write_settings(settings)
        print(f"added {added} hook entries to {SETTINGS_PATH} (backup at {backup})")
    else:
        print("hooks already installed")
    print("hooks take effect in sessions started from now on; running windows keep their old hook set")


def uninstall_hooks():
    settings = load_settings()
    hooks = settings.get("hooks", {})
    removed = 0
    for ev in list(hooks.keys()):
        kept = []
        for g in hooks[ev]:
            inner = [h for h in g.get("hooks", []) if HOOK_CMD not in (h.get("command") or "")]
            removed += len(g.get("hooks", [])) - len(inner)
            if inner:
                g["hooks"] = inner
                kept.append(g)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if removed:
        backup = write_settings(settings)
        print(f"removed {removed} hook entries (backup at {backup})")
    else:
        print("no session-board hooks found")


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    board: "Board" = None  # type: ignore[assignment]  (set in main)

    def log_message(self, format, *args):
        if os.environ.get("SESSION_BOARD_DEBUG"):
            super().log_message(format, *args)

    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def public_json(self):
        with self.board.lock:
            return self.board.public_json

    def send_raw_json(self, text, status=HTTPStatus.OK):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self):
        """Reject cross-site POSTs: any page could otherwise raise Terminal tabs."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        if origin and origin not in (f"http://{host}", f"http://127.0.0.1:{self.server.server_address[1]}", f"http://localhost:{self.server.server_address[1]}"):
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        return ctype == "application/json"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_file("index.html", "text/html; charset=utf-8")
        if path == "/api/snapshot":
            return self.send_raw_json(self.public_json())
        if path == "/events":
            return self.stream_events()
        if path.startswith("/static/"):
            name = os.path.basename(path)
            ctype = {"css": "text/css", "js": "text/javascript", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
            return self.send_file(name, ctype)
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_file(self, name, ctype):
        full = os.path.join(STATIC, name)
        if not os.path.isfile(full):
            return self.send_error(HTTPStatus.NOT_FOUND)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = -1
        try:
            while True:
                with self.board.cond:
                    self.board.cond.wait_for(lambda: self.board.version != last, timeout=15)
                    last = self.board.version
                payload = self.public_json()
                self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.same_origin():
            return self.send_json({"ok": False, "error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if path == "/api/focus":
            tty = body.get("tty")
            if not isinstance(tty, str) or not tty.startswith("ttys") or not tty[4:].isdigit():
                return self.send_json({"ok": False, "error": "no tty for this session"}, HTTPStatus.BAD_REQUEST)
            try:
                ok = focus_tty(tty)
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self.send_json({"ok": ok, "error": None if ok else "tab not found"})
        self.send_error(HTTPStatus.NOT_FOUND)


def poll_loop(board):
    while True:
        t0 = time.time()
        try:
            board.refresh()
        except Exception as e:
            log(f"refresh failed: {e}")
        time.sleep(max(0.2, POLL - (time.time() - t0)))


def board_running_at(url):
    """True if a session board is already answering at url (probes /api/snapshot)."""
    try:
        with urlopen(url + "api/snapshot", timeout=1.0) as r:
            return r.status == 200 and "sessions" in json.loads(r.read())
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("SESSION_BOARD_PORT", 7777)))
    ap.add_argument("--install-hooks", action="store_true")
    ap.add_argument("--uninstall-hooks", action="store_true")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = ap.parse_args()

    if args.install_hooks:
        return install_hooks()
    if args.uninstall_hooks:
        return uninstall_hooks()

    board = Board()
    if args.once:
        board.refresh()
        print(json.dumps(json.loads(board.public_json), indent=2))
        return

    Handler.board = board
    url = f"http://127.0.0.1:{args.port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        if board_running_at(url):
            log(f"session board already running on {url}")
            if not args.no_open:
                webbrowser.open(url)
            return
        log(f"port {args.port} is in use by something that is not a session board; pick another with --port")
        sys.exit(1)
    httpd.daemon_threads = True
    board.refresh()
    threading.Thread(target=poll_loop, args=(board,), daemon=True).start()
    log(f"session board on {url}")
    log(f"hooks installed: {hooks_installed()} · log: {LOG_PATH}")
    for k, v in board.errors.items():
        log(f"source {k}: {v}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
