"""MCP server exposing message boards to any MCP client (Claude Code, Codex, ...).

Each client session spawns its own stdio process; they all share one SQLite file,
so a message posted from a Codex session is visible to a Claude Code session immediately.

Token budget: every result lands in the agent's context and is re-read on later turns, and every
tool call is itself a full agent turn. So:
- results are terse plain text (and "caveman" style); posts return only an id
- the session remembers its board key and read cursor, so calls don't repeat them
- every post also delivers others' new messages, so separate read calls are rarely needed
- the session's own posts are never echoed back
- tool schemas are stripped of auto-generated titles and output schemas
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time

from mcp.server.mcpserver import MCPServer

from .compact import join_board as _join
from .claims import normalize
from .compact import render_entries, render_files
from .service import DEFAULT_AGENT, distill_in_background, sweep_stale_distills
from .store import BoardError, Store

INSTRUCTIONS = """Boards: agents in separate sessions (Claude Code, Codex) share one project.
Task start: search_memories. Have key: join_board. Else create_board, give user key.
Post while working: status (what you're doing; replaces your old one), decision (+why), question, answer (reply_to), \
blocker, promise (what you owe others, e.g. "post when X rebuilt"; keep it by replying to it), result, deadend (what \
failed + why). Commits are posted for you.
Files: don't name files you're working on; the inventory shows them (your own get stripped from status/notes). \
Editing claims a file for you; files(claim=[...]) before touching more, files(lock=[...]) to block others' edits, \
files(release=["*"]) when done. Committed files release themselves.
Write caveman: no articles, filler, pleasantries. Keep paths, negations, why. No made-up abbreviations. 1-3 lines.
Report outcomes, not metrics: "tests pass", not "474 pass, ruff+mypy clean". Numbers only when something fails.
Mention an agent by its tag (@3f2a) or name to get its attention; it's woken if idle. Your tag is in join's [you=...].
Others' new msgs arrive in post results. read_messages(wait_seconds) only when blocked on reply.
Done: close_board(outcome)."""

mcp = MCPServer("huddlehut", instructions=INSTRUCTIONS)
store = Store()
# One server process per client session; the suffix tells two sessions of the same tool apart.
AUTHOR = DEFAULT_AGENT if os.environ.get("HUDDLEHUT_EXACT_NAME") else f"{DEFAULT_AGENT}-{secrets.token_hex(2)}"
tool = mcp.tool(structured_output=False)  # plain text only: no output schema, no duplicate JSON result


class Session:
    """Per-process state: the active board. The read cursor lives in the database, keyed by this
    session's agent name, so the session's hooks see and advance the same cursor."""

    def __init__(self) -> None:
        self.key: str | None = None

    def use(self, key: str) -> str:
        key = key.strip() or self.key or ""
        if not key:
            raise BoardError("no board. join_board(key) or create_board first")
        self.key = key
        try:
            store.touch(store.board_for_key(key).id, AUTHOR)  # presence: this agent is active
        except BoardError:
            pass
        return key

    def inbox(self, key: str) -> tuple[list, int, str]:
        """Others' messages since this session (or its hooks) last looked; advances the cursor."""
        board = store.board_for_key(key)
        cursor = store.get_cursor(board.id, AUTHOR)
        if cursor is None:
            # Never joined: don't dump the whole history into a post result; join_board gives the
            # compact view instead.
            cursor = store.max_message_id(board.id)
            store.set_cursor(board.id, AUTHOR, cursor)
            return [], cursor, board.status
        new = store.unread(board.id, AUTHOR)
        if new:
            cursor = new[-1].id
            store.set_cursor(board.id, AUTHOR, cursor)
        return new, cursor, board.status


def _held_note(m) -> str:
    """A status claimed files someone else holds: say so instead of silently not claiming them."""
    held = getattr(m, "conflicts", None) or []
    return "".join(f"\nnot claimed: {p} held by {c['agent']}. Ask them first." for p, c in held)


def _marker(board_id: str) -> str:
    """First line of create/join results; hooks read it to link the client session to the board."""
    return f"[{board_id} you={AUTHOR}]"


S = Session()


@tool
def create_board(name: str, project: str, goal: str = "") -> str:
    """Spin up board. project: repo name."""
    existing = [b for b in store.list_boards(project) if b.name != name]
    board, key = store.create_board(name, project, AUTHOR, goal)
    S.use(key)
    store.set_cursor(board.id, AUTHOR, 0)
    out = f"{_marker(board.id)}\nCreated. Key: {key}\nGive user key for other sessions. Not recoverable."
    if existing:
        out += f"\nOther open boards {project}: " + ", ".join(f"{b.name} ({b.id})" for b in existing[:5])
    return out


@tool
async def join_board(key: str, status: str = "") -> str:
    """Join by key. Returns who does what, open questions, decision log. status: what you'll do."""
    try:
        S.use(key)
        note = ""
        if status:
            note = _held_note(store.post(key, AUTHOR, status, kind="status"))
        text, last_id = await asyncio.to_thread(_join, store.url, key)
        board = store.board_for_key(key)
        store.set_cursor(board.id, AUTHOR, last_id)
        return f"{_marker(board.id)}\n{text}{note}"
    except BoardError as e:
        return f"error: {e}"


@tool
def post(content: str, kind: str = "note", reply_to: int = 0, key: str = "") -> str:
    """kind: status|decision|question|answer|blocker|promise|result|deadend|note. reply_to: id answered/kept. Returns others' new msgs."""
    try:
        k = S.use(key)
        m = store.post(k, AUTHOR, content, kind, reply_to or None)
        new, _, _ = S.inbox(k)
    except BoardError as e:
        return f"error: {e}"
    return f"ok #{m.id}" + _held_note(m) + ("\nNew:\n" + render_entries(store, new) if new else "")


@tool
def files(claim: list[str] | None = None, lock: list[str] | None = None, unlock: list[str] | None = None,
          release: list[str] | None = None, key: str = "") -> str:
    """Shared file inventory. claim: files/globs you're starting on. lock: claim + block others' edits (lapses if you go idle or leave). unlock: keep claim, drop lock. release: done ("*" = all). Returns who holds what."""
    try:
        board = store.board_for_key(S.use(key))
        top = (store.presence(board.id).get(AUTHOR) or {}).get("toplevel")

        def norm(paths):
            return ["*"] if "*" in paths else [normalize(p, top) for p in paths]

        lines = []
        if unlock:
            store.unlock(board.id, AUTHOR, norm(unlock))
        if release:
            store.release(board.id, AUTHOR, norm(release))
        for paths, locking in ((claim, False), (lock, True)):
            for path, c in store.claim(board.id, AUTHOR, norm(paths), top, lock=locking) if paths else []:
                lines.append(f"not {'locked' if locking else 'claimed'}: {path} held by {c['agent']}. Ask them first.")
    except BoardError as e:
        return f"error: {e}"
    return "\n".join(lines + [render_files(store, board.id) or "(no files claimed)"])


@tool
async def read_messages(wait_seconds: int = 0, since_id: int = -1, key: str = "") -> str:
    """Others' new msgs. wait_seconds<=50 blocks for one. since_id: reread from id."""
    try:
        k = S.use(key)
        if since_id >= 0:
            store.set_cursor(store.board_for_key(k).id, AUTHOR, since_id, rewind=True)
        deadline = time.monotonic() + min(max(wait_seconds, 0), 50)
        while True:
            new, last, status = S.inbox(k)
            if new or status != "open" or time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)
    except BoardError as e:
        return f"error: {e}"
    tail = "" if status == "open" else f" (board {status})"
    return (render_entries(store, new) if new else "(nothing new)") + f"\nlast_id={last}{tail}"


@tool
def close_board(outcome: str = "", key: str = "") -> str:
    """Spin down when done. Saves lessons to memory. outcome: what worked/failed."""
    try:
        k = S.use(key)
        if outcome:
            store.post(k, AUTHOR, outcome, kind="result")
        board = store.close_board(k, AUTHOR, outcome)
    except BoardError as e:
        return f"error: {e}"
    distill_in_background(store.url, board.id)
    return "Closed. Saving lessons."


@tool
def reopen_board(key: str = "") -> str:
    """Spin closed board back up."""
    try:
        store.reopen_board(S.use(key), AUTHOR)
    except BoardError as e:
        return f"error: {e}"
    return "Reopened."


@tool
def search_memories(query: str, project: str = "", limit: int = 3) -> str:
    """Lessons from past boards."""
    hits = store.search_memories(query, project or None, limit)
    if not hits:
        return "(no lessons)"
    return "\n".join(f"- {h['title']}: {h['technique']} (when: {h['when_to_apply']})" for h in hits)


def _slim_schemas() -> None:
    """Drop pydantic's auto-generated "title" keys from every input schema: pure overhead for the
    model, repeated in every session's tool list."""

    def strip(node):
        if isinstance(node, dict):
            node.pop("title", None)
            for v in node.values():
                strip(v)
        elif isinstance(node, list):
            for v in node:
                strip(v)

    for t in getattr(getattr(mcp, "_tool_manager", None), "_tools", {}).values():
        strip(t.parameters)


_slim_schemas()


def main() -> None:
    sweep_stale_distills(store)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
