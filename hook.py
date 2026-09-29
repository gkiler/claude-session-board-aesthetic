"""Session Board hook logger.

Claude Code pipes the hook payload on stdin. This appends one trimmed JSON
line to the event log and always exits 0 so it can never block a session.
Large fields (tool_result, full prompts) are dropped or clipped.
"""
import json
import os
import sys
import time
import fcntl

LOG = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.claude/session-board/events.jsonl")


def clip(s, n=240):
    return s if not isinstance(s, str) or len(s) <= n else s[:n] + "…"


def main():
    try:
        raw = json.load(sys.stdin)
    except Exception:
        return
    out = {"ts": round(time.time(), 3)}
    for k in ("session_id", "hook_event_name", "cwd", "tool_name", "tool_use_id",
              "notification_type", "why", "how", "agent_id", "agent_type"):
        if k in raw:
            out[k] = raw[k]
    for k in ("message", "prompt", "user_prompt", "last_assistant_message"):
        if k in raw:
            out[k] = clip(raw[k])
    ti = raw.get("tool_input")
    if isinstance(ti, dict):
        keep = {}
        for k in ("command", "file_path", "pattern", "description", "url", "prompt", "skill", "subagent_type"):
            if k in ti:
                keep[k] = clip(ti[k], 160)
        if keep:
            out["tool_input"] = keep
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            trim_log()
            with open(LOG, "a") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception:
        pass


MAX_BYTES = 8_000_000
KEEP_BYTES = 1_000_000


def trim_log():
    """Keep the log bounded: past MAX_BYTES, rewrite it with only the last KEEP_BYTES of whole lines."""
    try:
        if os.path.getsize(LOG) < MAX_BYTES:
            return
    except OSError:
        return
    with open(LOG, "rb") as f:
        f.seek(-KEEP_BYTES, os.SEEK_END)
        tail = f.read()
    tail = tail[tail.find(b"\n") + 1:]
    tmp = LOG + ".tmp"
    with open(tmp, "wb") as f:
        f.write(tail)
    os.replace(tmp, LOG)


if __name__ == "__main__":
    main()
