# Session Board

A local dashboard for concurrent Claude Code and Codex sessions. One eye above the horizon,
one lantern per session. Yellow lanterns (the Sign) need you; click any lantern to raise
its Terminal tab.

Search by project, title, prompt, or session ID. Filter by provider or state, sort
attention-first (including children that need you), or switch to compact mode.
These preferences are saved in the browser. Details shows the latest activity,
parent session, prompt, model when available, and a copyable session ID. Claude
lanterns still raise their Terminal tab; Codex lanterns open Details.

## Run

    ./board                 # serves http://127.0.0.1:7777 and opens it
    ./board --port 8000     # different port
    ./board --no-open       # do not open the browser

Stdlib Python only. No build step, no dependencies.

After updating the code, restart the running server and reload the page. Launching
`./board` while an older server is running opens that existing server.

## Install the hooks (optional, one-time)

    python3 server.py --install-hooks
    python3 server.py --uninstall-hooks   # to remove them

Run the installer again after upgrading to add missing events. It preserves other
hooks and keeps the original settings backup. The board warns about partial installs.

Without hooks the board already shows every session, its state (working / resting /
needs you / blocked / gone), its Terminal tab title, and click-to-raise. Hooks add the
current tool and how long it has run, the permission or question that is waiting, the
last prompt, and how long a session has been resting. Hooks only apply to windows started
after installation. A backup of settings.json is written next to it on first install.

The hook appends one trimmed JSON line per event to `~/.claude/session-board/events.jsonl`
and its shell wrapper always exits 0. Writes and log rotation share a file lock so
concurrent agents do not overwrite each other's events. Hook execution has a 5-second
timeout. Delete the log file any time.

`SubagentStart` and `SubagentStop` add child cards under the Claude session. Tool
events carrying `agent_id` update the child without replacing the parent's tool.
Child lists are collapsible and expand when a child needs attention or is filtered.
Completed children remain for 90 seconds. Without recent child events, activity is
marked unconfirmed after five minutes. Claude lifecycle fields follow the
[official hook reference](https://code.claude.com/docs/en/hooks).

## States

| state     | source                                                                  |
|-----------|-------------------------------------------------------------------------|
| needs you | `claude agents` status `waiting`, or an unanswered permission / question notification |
| working   | status `busy`; foot shows the running tool from hooks                    |
| resting   | status `idle`; dims and sallows over the first half hour                 |
| blocked   | background agents whose state is `blocked`                              |
| gone      | vanished from `claude agents`; fades out after 90 s                     |
| unknown   | activity cannot be confirmed from recent records                       |

If a source fails, its last known sessions remain visible with a stale marker and
the source error. A failed query does not make every session disappear.

## Data sources

- `claude agents --json` every 2 s (name, cwd, pid, status, background state)
- `ps` maps each pid to its tty, Terminal.app (AppleScript) maps tty to tab title
- `events.jsonl` from the hooks, tailed incrementally
- Codex `state_*.sqlite`, `thread_history_*.sqlite`, and rollout JSONL under
  `~/.codex` (or `CODEX_HOME`), opened read-only. No Codex hooks or configuration edits.

Codex shows up to 100 non-archived threads updated in the last 24 hours, plus parents
of those threads. Recorded `thread_spawn_edges` supply parent/child relationships;
legacy rollout spawn metadata is also recognized. Both legacy JSONL and paginated
SQLite history are supported. These local formats can change with Codex versions;
read/schema failures appear as source errors.

Codex states describe the last recorded turn, not process liveness. An unfinished
turn with no recorded activity for five minutes becomes unconfirmed. Closed sessions
can remain in the recent list. Codex permission prompts and Terminal tab matching
are not available from this adapter. The board does not infer parentage merely
because sessions share a project.

Terminal.app only. iTerm2 or tmux sessions still show up, but without a tab title and
without click-to-raise.

## Files

- `server.py`     poller, merge, SSE stream, focus endpoint, hook installer
- `codex_source.py`     read-only Codex history adapter
- `hook.sh` / `hook.py`  the hook logger
- `static/index.html`    the page (eye, starfield, lanterns)
- `board`         launcher

## Verification

    python3 -m unittest -v test_board
    python3 -m py_compile server.py hook.py codex_source.py

Optional UI checks use an existing Playwright installation and browser (neither is
required to run the board):

    node test_ui.cjs /path/to/playwright [/path/to/chromium]

The UI check uses fixture sessions, tests nested filtering, details, text escaping,
saved preferences, reconnects, and mobile layout, and saves screenshots in `/tmp`.
