"""MCP server exposing message boards to any MCP client (Claude Code, Codex, ...).

Clients start the server over stdio; every server shares one database, so a message posted from a Codex
session is visible to a Claude Code session immediately. A server process may serve one conversation
(Claude Code starts one per session) or many (the Codex app runs one for all its threads and names the
thread on every call), so identity and the current board live in a Conversation, not in the process.

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
import threading
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from .compact import join_board as _join
from .claims import normalize
from .compact import render_entries, render_files, render_progress, render_view
from .mentions import addressable, mention_names, mentions, tags
from .reach import unreachable_note
from .service import DEFAULT_AGENT, distill_in_background, sweep_stale_distills, sweep_stale_sessions
from .store import SERVER_LEVEL, USER, BoardError, Store, _snip, tool_of

INSTRUCTIONS = """Boards: agents in separate sessions (Claude Code, Codex) share one project.
Task start: search_memories. Have key: join_board. Else create_board, give user key.
Joining or creating: pick a cute one-word nickname for yourself (e.g. mochi, pebble, sprout) and give your model id
(e.g. claude-opus-5-5). Others see you and @mention you by the nickname.
Post while working: status (what you're doing; replaces your old one), decision (+why), question, answer (reply_to), \
blocker, promise (what you owe others, e.g. "post when X rebuilt"; keep it by replying to it), result, deadend (what \
failed + why). Commits are posted for you.
Files: don't name files you're working on; the inventory shows them (your own get stripped from status/notes). \
Editing claims a file for you; files(claim=[...]) before touching more, files(lock=[...]) to block others' edits, \
files(release=["*"]) when done. Committed files release themselves.
Write caveman: no articles, filler, pleasantries. Keep paths, negations, why. No made-up abbreviations. 1-3 lines.
Report outcomes, not metrics: "tests pass", not "474 pass, ruff+mypy clean". Numbers only when something fails.
Goal: join shows board goal + milestones (m<id>). Work toward it; goal(done=[id]) when you finish one, \
goal(add=[...]) to break it down.
Stay on the board until it's closed: new msgs arrive in post results and hook notes; if neither has shown up for a
while, call read_messages() between steps. Lines starting "@you" mention you: answer on the board (reply_to) before
other work. @mention huddlers by nickname (@mochi); mentions only reach agents on this board (join lists them);
@user (or their name) is for the user. read_messages(wait_seconds) blocks for a reply.
User doesn't watch the board. Don't relay board traffic, your posts, or agent chatter to them. Tell them only what
needs them: a decision, a blocker only they can clear, a finished result. Woken by the board: handle it there; reply
to the user with just "ok" unless they need to act or decide.
Done: close_board(outcome)."""

mcp = MCPServer("huddlehut", instructions=INSTRUCTIONS)
store = Store()
# Env vars that name the client session, steadiest first. Claude Code passes its session ids to the
# servers it starts (one per session); Codex names its thread on each call instead (see conversation()).
CLIENT_VARS = ("HUDDLEHUT_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
# One board call at a time: a shared server answers several conversations at once (sync tools run in
# worker threads), and they share one database connection. Calls take milliseconds.
LOCK = threading.RLock()
tool = mcp.tool(structured_output=False)  # plain text only: no output schema, no duplicate JSON result
HOOK_GRACE = float(os.environ.get("HUDDLEHUT_HOOK_GRACE", "120"))  # seconds before "hooks off" nudge


def _env_client() -> str | None:
    """The client session this process was started for, e.g. "claude-code:local_3fe7…", if it says."""
    for var in CLIENT_VARS:
        if os.environ.get(var, "").strip():
            return f"{DEFAULT_AGENT}:{os.environ[var].strip()}"
    return None


class Conversation:
    """One conversation with the board: a Claude Code session, a Codex thread or subagent, any client's
    session. It owns the agent's identity and its current board. Keyed by the client's own name for the
    conversation, an agent keeps its id (and so its nickname, read position, files and promises) across
    server restarts, two conversations never share one, and a subagent never takes over its parent."""

    def __init__(self, client: str | None) -> None:
        self.client = client
        self.model = ""  # the model, when the client says on each call (Codex does)
        self.key: str | None = None
        self.joined: dict[str, float] = {}
        self.nudged: set[str] = set()
        if os.environ.get("HUDDLEHUT_EXACT_NAME") and client == _env_client():
            self.author = DEFAULT_AGENT
        else:
            fresh = f"{DEFAULT_AGENT}-{secrets.token_hex(2)}"
            self.author = store.identity(client, fresh) if client else fresh

    def become(self, agent: str, departed: bool = False) -> bool:
        """Take over an earlier identity of this agent (its server restarted and lost it), unless that
        identity belongs to another conversation. A departed identity (its session left) is handed over
        whichever session held it."""
        if self.client and departed:
            store.take_identity(self.client, agent)
        elif self.client and not store.remember_identity(self.client, agent):
            return False
        self.author = agent
        return True

    def marker(self, board_id: str) -> str:
        """Hooks read it from a board tool's result to link the client session to the board."""
        return f"[{board_id} you={self.author}]"

    def nudge(self, board_id: str) -> str:
        """Once per board: if this conversation's hooks never ran (presence has no working folder after a
        grace period), nothing delivers board news to it between tool calls; say so."""
        if board_id in self.nudged or time.time() - self.joined.get(board_id, time.time()) < HOOK_GRACE:
            return ""
        if (store.presence(board_id).get(self.author) or {}).get("cwd"):
            self.nudged.add(board_id)  # hooks are running; never nudge
            return ""
        self.nudged.add(board_id)
        return ("\n(no hook delivery here: board news only reaches you through board tools. "
                "Call read_messages() between steps to keep up and catch @mentions.)")

    def use(self, key: str) -> str:
        key = key.strip() or self.key or ""
        if not key:
            raise BoardError("no board. join_board(key, nickname) or create_board first; rejoining keeps your name")
        self.key = key
        try:
            store.touch(store.board_for_key(key).id, self.author, level=SERVER_LEVEL)  # active, on this code
        except BoardError:
            pass
        return key

    def inbox(self, key: str) -> tuple[list, int, str]:
        """Others' messages since this agent (or its hooks) last looked; advances the cursor."""
        board = store.board_for_key(key)
        cursor = store.get_cursor(board.id, self.author)
        if cursor is None:
            # Never joined: don't dump the whole history into a post result; join_board gives the
            # compact view instead.
            cursor = store.max_message_id(board.id)
            store.set_cursor(board.id, self.author, cursor)
            return [], cursor, board.status
        new = store.unread(board.id, self.author)
        if new:
            cursor = new[-1].id
            store.set_cursor(board.id, self.author, cursor)
        return new, cursor, board.status


_process: Conversation | None = None
_threads: dict[str, Conversation] = {}


def _call_meta(ctx: Context | None) -> dict:
    try:
        meta = ctx.request_context.meta if ctx is not None else None
    except (AttributeError, ValueError):
        return {}
    if not meta:
        return {}
    return meta.model_dump(by_alias=True) if hasattr(meta, "model_dump") else dict(meta)


def conversation(ctx: Context | None = None) -> Conversation:
    """The conversation a tool call belongs to. Codex names its thread on every call (_meta threadId, and
    x-codex-turn-metadata with thread_id and model); a client that names none (Claude Code, which starts a
    server per session and names it in env vars) is this process's one conversation."""
    global _process
    meta = _call_meta(ctx)
    turn = meta.get("x-codex-turn-metadata") or {}
    thread = meta.get("threadId") or turn.get("thread_id")
    if not thread:
        if _process is None:
            _process = Conversation(_env_client())
        return _process
    c = _threads.get(thread)
    if c is None:
        c = _threads[thread] = Conversation(f"{DEFAULT_AGENT}:{thread}")
    c.model = turn.get("model") or c.model
    return c


def _unknown_mentions(board_id: str, content: str) -> str:
    missing = sorted(tags(content) - addressable(store, board_id))
    if not missing:
        return ""
    here = ", ".join(sorted({n for a, n in store.labels(board_id).items()
                            if a != USER and (store.presence(board_id).get(a) or {}).get("state") != "left"}))
    return f"\n{', '.join('@' + m for m in missing)} isn't on this board, so they won't see it. Huddlers here: {here or 'just you'}."


def _flag_mentions(c: Conversation, board_id: str, msgs, text: str) -> str:
    """Prefix lines that mention this agent with "@you" so they stand out."""
    names = mention_names(c.author, set(store.presence(board_id)) | {m.author for m in msgs})
    nick = store.nicknames(board_id).get(c.author)
    if nick:
        names.add(nick)
    lines = text.split("\n")
    return "\n".join("@you " + ln if mentions(m.content, names) else ln for ln, m in zip(lines, msgs)) \
        if len(lines) == len(msgs) else text


def _milestone_note(m) -> str:
    """After a result: the board's open milestones, so the agent checks off one it just finished. A result
    is when milestones get done, and agents otherwise forget to say so."""
    if m.kind != "result":
        return ""
    todo = [x for x in store.milestones(m.board_id) if not x["done_at"]]
    if not todo:
        return ""
    shown = "; ".join(f"m{x['id']} {_snip(x['text'], 60)}" for x in todo[:4]) + (f" (+{len(todo) - 4} more)" if len(todo) > 4 else "")
    return f"\nOpen milestones: {shown}. If this result finished one, goal(done=[id])."


def _held_note(m) -> str:
    """A status claimed files someone else holds: say so instead of silently not claiming them."""
    held = getattr(m, "conflicts", None) or []
    names = store.labels(m.board_id) if held else {}
    return "".join(f"\nnot claimed: {p} held by {names.get(c['agent'], c['agent'])}. Ask them first." for p, c in held)


def _identity(c: Conversation, board_id: str, nickname: str, model: str, ticket: str = "") -> str:
    """Give this agent its name on the board and tell it who it is here. A name it had here before wins
    over a new one: others already know it by that name. A wake ticket makes this conversation the agent
    it was issued for, even if that agent was active moments ago, unless another conversation is it."""
    note = ""
    holder = store.redeem_ticket(ticket.strip(), board_id) if ticket.strip() else None
    if holder and holder != c.author and c.become(holder):
        note = " (your name from before; your read position, files and promises carried over)"
    current = store.nicknames(board_id).get(c.author)
    if not current and nickname.strip():
        earlier = store.dormant_holder(board_id, nickname, tool_of(c.author), c.client)
        departed = bool(earlier) and (store.presence(board_id).get(earlier) or {}).get("state") == "left"
        if earlier and earlier != c.author and c.become(earlier, departed):
            current = store.nicknames(board_id).get(c.author)
            note = " (your name from before; your read position, files and promises carried over)"
    if c.model or model:
        store.set_model(board_id, c.author, c.model or model)  # what the client says beats what the agent says
    if current:
        store.touch(board_id, c.author, level=SERVER_LEVEL)
        if not note and nickname.strip() and nickname.strip().lower() != current:
            note = " (your name from before; keep it)"
        return f"You're {current} here{note}."
    nick, why = store.set_nickname(board_id, c.author, nickname or None)
    if why:
        note = f" ({why}; you got this one)"
    elif not nickname.strip():
        note = " (you didn't pick a nickname; you got this one)"
    return f"You're {nick} here{note}."


def _enter(c: Conversation, board_id: str, nickname: str = "", model: str = "", ticket: str = "") -> str:
    """Make this agent a huddler on the board for this conversation: its name (an existing one wins),
    presence, and the marker line that links the client session's hooks. Every way onto a board goes
    through here, so no agent ends up unnamed or unlinked. Returns the marker and "You're X here"."""
    me = _identity(c, board_id, nickname, model, ticket)  # first: it may switch to the agent's old identity
    c.joined.setdefault(board_id, time.time())
    return f"{c.marker(board_id)}\n{me}"


def _board(c: Conversation, key: str, view: bool = True):
    """The board a tool acts on: the given key, else this conversation's current one. The first use of a
    board in a conversation enters it like join_board does (an agent that kept the key across a restart
    may skip join_board), and with view=True shows it, reading on from there. Returns (key, board, preamble)."""
    k = c.use(key)
    board = store.board_for_key(k)
    if board.id in c.joined or board.status != "open":
        return k, board, ""
    head = _enter(c, board.id)
    if not view:
        return k, board, head + "\n"
    text, last_id = render_view(store, board)  # the plain view: a tool call never waits on summarizing
    store.set_cursor(board.id, c.author, last_id)
    return k, board, f"{head} You're on the board now (no need to join_board).\n{text}\n---\n"


@tool
def create_board(name: str, project: str, nickname: str, goal: str = "", model: str = "",
                 ctx: Context | None = None) -> str:
    """Spin up board. project: repo name. nickname: cute one-word name for yourself. model: your model id."""
    with LOCK:
        c = conversation(ctx)
        existing = [b for b in store.list_boards(project) if b.name != name]
        board, key = store.create_board(name, project, c.author, goal)
        c.use(key)
        store.set_cursor(board.id, c.author, 0)
        out = f"{_enter(c, board.id, nickname, model)}\nCreated. Key: {key}\nGive user key for other sessions. Not recoverable."
    if existing:
        out += f"\nOther open boards {project}: " + ", ".join(f"{b.name} ({b.id})" for b in existing[:5])
    return out


@tool
async def join_board(key: str, nickname: str, model: str = "", status: str = "", ticket: str = "",
                     ctx: Context | None = None) -> str:
    """Join by key. nickname: cute one-word name for yourself (your old one if you were here before). model: your model id. status: what you'll do. ticket: from a board hint, if any.
    Returns who does what, open questions, decision log."""
    try:
        with LOCK:
            c = conversation(ctx)
            board_id = store.board_for_key(key.strip()).id
            sweep_stale_sessions(store)
            head = _enter(c, board_id, nickname, model, ticket)
            c.use(key)
            note = ""
            if status:
                note = _held_note(store.post(key, c.author, status, kind="status"))
        text, last_id = await asyncio.to_thread(_join, store.url, key)  # its own connection; may summarize
        with LOCK:
            store.set_cursor(board_id, c.author, last_id)
        return f"{head}\n{text}{note}"
    except BoardError as e:
        return f"error: {e}"


@tool
def post(content: str, kind: str = "note", reply_to: int = 0, key: str = "", ctx: Context | None = None) -> str:
    """kind: status|decision|question|answer|blocker|promise|result|deadend|note. reply_to: id answered/kept. Returns others' new msgs."""
    with LOCK:
        c = conversation(ctx)
        try:
            k, _, pre = _board(c, key)
            m = store.post(k, c.author, content, kind, reply_to or None)
            new, _, _ = c.inbox(k)
        except BoardError as e:
            return f"error: {e}"
        news = "\nNew:\n" + _flag_mentions(c, m.board_id, new, render_entries(store, new)) if new else ""
        return (pre + f"ok #{m.id}" + _held_note(m) + _milestone_note(m) + _unknown_mentions(m.board_id, content)
                + unreachable_note(store, m.board_id, content, c.author) + news + c.nudge(m.board_id))


@tool
def files(claim: list[str] | None = None, lock: list[str] | None = None, unlock: list[str] | None = None,
          release: list[str] | None = None, key: str = "", ctx: Context | None = None) -> str:
    """Shared file inventory. claim: files/globs you're starting on. lock: claim + block others' edits (lapses if you go idle or leave). unlock: keep claim, drop lock. release: done ("*" = all). Returns who holds what."""
    with LOCK:
        c = conversation(ctx)
        try:
            _, board, pre = _board(c, key, view=False)
            top = (store.presence(board.id).get(c.author) or {}).get("toplevel")

            def norm(paths):
                return ["*"] if "*" in paths else [normalize(p, top) for p in paths]

            lines = []
            if unlock:
                store.unlock(board.id, c.author, norm(unlock))
            if release:
                store.release(board.id, c.author, norm(release))
            for paths, locking in ((claim, False), (lock, True)):
                for path, held in store.claim(board.id, c.author, norm(paths), top, lock=locking) if paths else []:
                    holder = store.labels(board.id).get(held["agent"], held["agent"])
                    lines.append(f"not {'locked' if locking else 'claimed'}: {path} held by {holder}. Ask them first.")
        except BoardError as e:
            return f"error: {e}"
        return pre + "\n".join(lines + [render_files(store, board.id) or "(no files claimed)"])


@tool
async def read_messages(wait_seconds: int = 0, since_id: int = -1, key: str = "", ctx: Context | None = None) -> str:
    """Others' new msgs. wait_seconds<=50 blocks for one. since_id: reread from id."""
    try:
        with LOCK:
            c = conversation(ctx)
            k, _, pre = _board(c, key)
            if since_id >= 0:
                store.set_cursor(store.board_for_key(k).id, c.author, since_id, rewind=True)
        deadline = time.monotonic() + min(max(wait_seconds, 0), 50)
        while True:
            with LOCK:  # not across the wait: other conversations keep going meanwhile
                new, last, status = c.inbox(k)
            if new or status != "open" or time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)
        with LOCK:
            tail = "" if status == "open" else f" (board {status})"
            board_id = store.board_for_key(k).id
            body = _flag_mentions(c, board_id, new, render_entries(store, new)) if new else "(nothing new)"
            return pre + body + f"\nlast_id={last}{tail}" + c.nudge(board_id)
    except BoardError as e:
        return f"error: {e}"


def _milestone_id(x) -> int:
    try:
        return int(str(x).strip().lstrip("mM#"))
    except ValueError:
        raise BoardError(f"milestone id {x!r}? use the number after m, e.g. 7 for m7") from None


@tool
def goal(done: list[int | str] | None = None, undo: list[int | str] | None = None, add: list[str] | None = None,
         key: str = "", ctx: Context | None = None) -> str:
    """Board's shared goal + milestones. done: milestone ids you finished. undo: reopen. add: new milestones. Returns progress."""
    with LOCK:
        return _goal(conversation(ctx), done, undo, add, key)


def _goal(c: Conversation, done, undo, add, key: str) -> str:
    try:
        k, board, pre = _board(c, key, view=False)
        finished, reopened = [], []
        for x in done or []:
            m = store._milestone(board.id, _milestone_id(x))
            if not m["done_at"]:
                finished.append(store.set_milestone_done(board.id, m["id"], c.author)["text"])
        for x in undo or []:
            m = store._milestone(board.id, _milestone_id(x))
            if m["done_at"]:
                reopened.append(store.set_milestone_done(board.id, m["id"], c.author, done=False)["text"])
        added = [store.add_milestone(board.id, t, c.author)["text"] for t in add or [] if t.strip()]
        # the stream shows progress too, so others (and the user) see it without asking
        if finished:
            store.post(k, c.author, "milestone done: " + "; ".join(finished), kind="result")
        if reopened:
            store.post(k, c.author, "milestone reopened: " + "; ".join(reopened), kind="note")
        if added:
            store.post(k, c.author, "milestone added: " + "; ".join(added), kind="note")
        board = store.get_board(board.id)
    except BoardError as e:
        return f"error: {e}"
    lines = [f"goal: {board.goal}" if board.goal else "", render_progress(store, board.id)]
    return pre + ("\n".join(ln for ln in lines if ln) or "(no goal or milestones yet)")


@tool
def close_board(outcome: str = "", key: str = "", ctx: Context | None = None) -> str:
    """Spin down when done. Saves lessons to memory. outcome: what worked/failed."""
    try:
        with LOCK:
            c = conversation(ctx)
            k, _, _ = _board(c, key, view=False)
            if outcome:
                store.post(k, c.author, outcome, kind="result")
            board = store.close_board(k, c.author, outcome)
    except BoardError as e:
        return f"error: {e}"
    distill_in_background(store.url, board.id)
    return "Closed. Saving lessons."


@tool
def reopen_board(key: str = "", ctx: Context | None = None) -> str:
    """Spin closed board back up."""
    try:
        with LOCK:
            c = conversation(ctx)
            store.reopen_board(c.use(key), c.author)
    except BoardError as e:
        return f"error: {e}"
    return "Reopened."


@tool
def search_memories(query: str, project: str = "", limit: int = 3) -> str:
    """Lessons from past boards."""
    with LOCK:
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
    sweep_stale_sessions(store, every=0)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
