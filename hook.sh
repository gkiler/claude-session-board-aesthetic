#!/bin/sh
# Session Board hook: append one trimmed JSON line per Claude Code hook event.
# Never blocks a session: swallows every error and always exits 0.
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${SESSION_BOARD_LOG:-$HOME/.claude/session-board/events.jsonl}"
python3 "$DIR/hook.py" "$LOG" 2>/dev/null
exit 0
