"""Client hooks: board messages reach an agent without it spending a turn on a tool call.

One entry point for Claude Code and Codex (`--client` picks the output format):

  post-tool      after every tool call. Links the client session to a board when the agent joins or
                 creates one (from the "[b_… you=…]" marker in the result), then delivers others'
                 unread messages as extra context. Checks at most every HUDDLEHUT_HOOK_INTERVAL s.
  session-start  after compaction or resume: re-injects the board view (no model call).
  stop           before the agent ends its turn: if a question, blocker or reply to it is unread,
                 keeps it going with those messages (once; guarded by stop_hook_active).
  wait           Claude Code only, as an asyncRewake Stop hook: after the agent goes idle, waits up
                 to HUDDLEHUT_WAIT_SECONDS for something that needs it, then wakes it (exit 2).

The read cursor is the same one the session's MCP server uses (cursors table, keyed by the agent
name), so nothing is delivered twice. Sessions that never joined a board exit before touching the
database, and nothing heavy is imported, so the hook adds little to each tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.environ.get("HUDDLEHUT_HOME", Path.home() / ".huddlehut"))
SESSIONS = HOME / "sessions"
MARKER = re.compile(r"\[(b_[0-9a-f]{8}) you=([\w.-]+)\]")
BOARD_TOOL = re.compile(r"^mcp__.*huddlehut.*__")
INTERVAL = float(os.environ.get("HUDDLEHUT_HOOK_INTERVAL", "5"))
WAIT_SECONDS = float(os.environ.get("HUDDLEHUT_WAIT_SECONDS", "1800"))
WAIT_POLL = float(os.environ.get("HUDDLEHUT_WAIT_POLL", "10"))
WAIT_GRACE = 3  # let the synchronous Stop hook claim anything already unread
MAX_LINES = 15
MAX_CHARS = 4000  # under Codex's ~2.5K-token and Claude Code's 10K-char hook output caps


# ---- per-client-session link state (a small local file) ---------------------


def _state_path(session_id: str) -> Path:
    return SESSIONS / (re.sub(r"[^\w.-]", "_", session_id) + ".json")


def load(session_id: str) -> dict:
    try:
        return json.loads(_state_path(session_id).read_text())
    except (OSError, ValueError):
        return {}


def save(session_id: str, state: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(session_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(_state_path(session_id))


def unlink(session_id: str) -> None:
    _state_path(session_id).unlink(missing_ok=True)


# ---- message selection and rendering ----------------------------------------


def _render(store, msgs) -> str:
    from .compact import render_entries

    omitted = max(0, len(msgs) - MAX_LINES)
    text = render_entries(store, msgs[omitted:])
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:].split("\n", 1)[-1]
    if omitted:
        text = f"(+{omitted} earlier: read_messages(since_id={msgs[0].id - 1}))\n" + text
    return text


def _needs_me(store, board_id: str, me: str, msgs) -> list:
    """Unread messages that call for this agent: questions, blockers, replies to it, mentions."""
    mine = {r["id"] for r in store._all("SELECT id FROM messages WHERE board_id=? AND author=?", (board_id, me))}
    return [m for m in msgs if m.kind in ("question", "blocker") or m.reply_to in mine or me in m.content]


def _take_unread(store, state: dict):
    """Unread messages from others; advances the shared cursor past them."""
    msgs = store.unread(state["board"], state["me"])
    if msgs:
        store.set_cursor(state["board"], state["me"], msgs[-1].id)
    return msgs


def _store():
    from .store import Store

    return Store()


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


# ---- events -----------------------------------------------------------------


def post_tool(inp: dict, client: str) -> dict | None:
    sid = inp.get("session_id") or ""
    tool = inp.get("tool_name") or ""
    now = time.time()
    if BOARD_TOOL.match(tool):
        m = MARKER.search(json.dumps(inp.get("tool_response"), ensure_ascii=False))
        state = load(sid)
        if m:
            state = {"board": m.group(1), "me": m.group(2), "checked": now}
        if state:
            state["active"] = now
            save(sid, state)
        return None  # the board tool's own result already carried any new messages
    state = load(sid)
    if not state:
        return None  # not on a board: no database access at all
    state["active"] = now
    if now - state.get("checked", 0) < INTERVAL:
        save(sid, state)
        return None
    state["checked"] = now
    save(sid, state)
    store = _store()
    board = store.get_board(state["board"])
    msgs = _take_unread(store, state)
    if board.status != "open":
        unlink(sid)
        extra = "\n" + _render(store, msgs) if msgs else ""
        return _context("PostToolUse", f"[huddlehut] board {board.name} was closed.{extra}")
    if not msgs:
        return None
    return _context("PostToolUse", f"[huddlehut {board.name}] new from others:\n" + _render(store, msgs))


def session_start(inp: dict, client: str) -> dict | None:
    source = inp.get("source")
    sid = inp.get("session_id") or ""
    state = load(sid)
    if source not in ("compact", "resume") or not state:
        return None
    from .compact import render_view

    store = _store()
    board = store.get_board(state["board"])
    if board.status != "open":
        unlink(sid)
        return None
    text, last_id = render_view(store, board)
    store.set_cursor(board.id, state["me"], last_id)
    tail = "" if source == "compact" else "\nSession resumed: join_board(key) again before posting."
    return _context("SessionStart", f"[huddlehut] you are {state['me']} on this board:\n{text}{tail}")


def stop(inp: dict, client: str) -> dict | None:
    if inp.get("stop_hook_active"):
        return None  # already continued once for this stop; don't loop
    sid = inp.get("session_id") or ""
    state = load(sid)
    if not state:
        return None
    store = _store()
    msgs = store.unread(state["board"], state["me"])
    urgent = _needs_me(store, state["board"], state["me"], msgs)
    if not urgent:
        return None
    store.set_cursor(state["board"], state["me"], msgs[-1].id)
    text = "[huddlehut] unread, needs you before stopping:\n" + _render(store, msgs)
    if client == "codex":
        return {"decision": "block", "reason": text}
    return _context("Stop", text)


def wait(inp: dict, client: str) -> int:
    """Background waiter (Claude Code asyncRewake): exit 2 + stderr wakes the idle agent."""
    sid = inp.get("session_id") or ""
    state = load(sid)
    if not state:
        return 0
    token, started = uuid.uuid4().hex, time.time()
    state["waiter"] = token
    save(sid, state)
    time.sleep(WAIT_GRACE)
    store = _store()
    while time.time() - started < WAIT_SECONDS:
        state = load(sid)
        if not state or state.get("waiter") != token or state.get("active", 0) > started:
            return 0  # a newer waiter took over, or the agent is working again (post-tool delivers)
        msgs = store.unread(state["board"], state["me"])
        if _needs_me(store, state["board"], state["me"], msgs):
            store.set_cursor(state["board"], state["me"], msgs[-1].id)
            print("[huddlehut] reply arrived:\n" + _render(store, msgs), file=sys.stderr)
            return 2
        if store.get_board(state["board"]).status != "open":
            return 0
        time.sleep(WAIT_POLL)
    return 0


EVENTS = {"post-tool": post_tool, "session-start": session_start, "stop": stop}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    client = argv[argv.index("--client") + 1] if "--client" in argv else "claude"
    try:
        inp = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        if event == "wait":
            return wait(inp, client)
        handler = EVENTS.get(event)
        out = handler(inp, client) if handler else None
        if out:
            print(json.dumps(out))
    except Exception as e:  # a hook must never break the agent's session
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            with open(HOME / "hook.log", "a") as f:
                f.write(f"{time.strftime('%F %T')} {event}: {type(e).__name__}: {e}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
