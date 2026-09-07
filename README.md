# Session Board

A local dashboard for many concurrent Claude Code windows. One eye above the horizon,
one lantern per session. Yellow lanterns (the Sign) need you; click any lantern to raise
its Terminal tab.

## Run

    ./board                 # serves http://127.0.0.1:7777 and opens it
    ./board --port 8000     # different port
    ./board --no-open       # do not open the browser

Stdlib Python only. No build step, no dependencies.

## Install the hooks (optional, one-time)

    python3 server.py --install-hooks
    python3 server.py --uninstall-hooks   # to remove them

Without hooks the board already shows every session, its state (working / resting /
needs you / blocked / gone), its Terminal tab title, and click-to-raise. Hooks add the
current tool and how long it has run, the permission or question that is waiting, the
last prompt, and how long a session has been resting. Hooks only apply to windows started
after installation. A backup of settings.json is written next to it on first install.

The hook appends one trimmed JSON line per event to `~/.claude/session-board/events.jsonl`
and always exits 0, so it can never block a session. Delete the file any time.

## States

| state     | source                                                                  |
|-----------|-------------------------------------------------------------------------|
| needs you | `claude agents` status `waiting`, or an unanswered permission / question notification |
| working   | status `busy`; foot shows the running tool from hooks                    |
| resting   | status `idle`; dims and sallows over the first half hour                 |
| blocked   | background agents whose state is `blocked`                              |
| gone      | vanished from `claude agents`; fades out after 90 s                     |

## Data sources

- `claude agents --json` every 2 s (name, cwd, pid, status, background state)
- `ps` maps each pid to its tty, Terminal.app (AppleScript) maps tty to tab title
- `events.jsonl` from the hooks, tailed incrementally

Terminal.app only. iTerm2 or tmux sessions still show up, but without a tab title and
without click-to-raise.

## Files

- `server.py`     poller, merge, SSE stream, focus endpoint, hook installer
- `hook.sh` / `hook.py`  the hook logger
- `static/index.html`    the page (eye, starfield, lanterns)
- `board`         launcher
