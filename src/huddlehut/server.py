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
from .compact import render_entries, render_files, render_progress, render_view
from .mentions import addressable, mention_names, mentions, tags
from .reach import unreachable_note
from .service import DEFAULT_AGENT, distill_in_background, sweep_stale_distills, sweep_stale_sessions
from .store import USER, BoardError, Store, _snip, tool_of

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
# servers it starts; Codex passes none, so Codex agents get their identity back by nickname instead.
CLIENT_VARS = ("HUDDLEHUT_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def _client() -> str | None:
    """This server's client session, e.g. "claude-code:local_3fe7…", or None if the client doesn't say."""
    for var in CLIENT_VARS:
        if os.environ.get(var, "").strip():
            return f"{DEFAULT_AGENT}:{os.environ[var].strip()}"
    return None


def _author() -> str:
    """This agent's id. One server process per client session; the suffix tells two sessions of the same
    tool apart. A session the database has seen before gets its old id back, so a restarted or resumed
    session keeps its nickname, read position, files and promises."""
    if os.environ.get("HUDDLEHUT_EXACT_NAME"):
        return DEFAULT_AGENT
    fresh = f"{DEFAULT_AGENT}-{secrets.token_hex(2)}"
    return store.identity(CLIENT, fresh) if CLIENT else fresh


CLIENT = _client()
AUTHOR = _author()
tool = mcp.tool(structured_output=False)  # plain text only: no output schema, no duplicate JSON result


HOOK_GRACE = float(os.environ.get("HUDDLEHUT_HOOK_GRACE", "120"))  # seconds before "hooks off" nudge
def _unknown_mentions(board_id: str, content: str) -> str:
    missing = sorted(tags(content) - addressable(store, board_id))
    if not missing:
        return ""
    here = ", ".join(sorted({n for a, n in store.labels(board_id).items()
                            if a != USER and (store.presence(board_id).get(a) or {}).get("state") != "left"}))
    return f"\n{', '.join('@' + m for m in missing)} isn't on this board, so they won't see it. Huddlers here: {here or 'just you'}."


def _flag_mentions(board_id: str, msgs, text: str) -> str:
    """Prefix lines that mention this agent with "@you" so they stand out."""
    names = mention_names(AUTHOR, set(store.presence(board_id)) | {m.author for m in msgs})
    nick = store.nicknames(board_id).get(AUTHOR)
    if nick:
        names.add(nick)
    lines = text.split("\n")
    return "\n".join("@you " + ln if mentions(m.content, names) else ln for ln, m in zip(lines, msgs)) \
        if len(lines) == len(msgs) else text


class Session:
    """Per-process state: the active board. The read cursor lives in the database, keyed by this
    session's agent name, so the session's hooks see and advance the same cursor."""

    def __init__(self) -> None:
        self.key: str | None = None
        self.joined: dict[str, float] = {}
        self.nudged: set[str] = set()

    def nudge(self, board_id: str) -> str:
        """Once per board: if this session's hooks never ran (presence has no working folder after a
        grace period), nothing delivers board news to it between tool calls; say so."""
        if board_id in self.nudged or time.time() - self.joined.get(board_id, time.time()) < HOOK_GRACE:
            return ""
        if (store.presence(board_id).get(AUTHOR) or {}).get("cwd"):
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


def _become(agent: str) -> None:
    """Take over an earlier identity of this agent (the server restarted and lost it)."""
    global AUTHOR
    AUTHOR = agent
    if CLIENT:
        store.remember_identity(CLIENT, agent)


def _identity(board_id: str, nickname: str, model: str, ticket: str = "") -> str:
    """Give this agent its name on the board and tell it who it is here. A name it had here before wins
    over a new one: others already know it by that name. A wake ticket (from the wake service's message)
    makes this session the agent it was issued for, even if that agent was active moments ago."""
    note = ""
    holder = store.redeem_ticket(ticket.strip(), board_id) if ticket.strip() else None
    if holder and holder != AUTHOR:
        _become(holder)
        note = " (your name from before; your read position, files and promises carried over)"
    current = store.nicknames(board_id).get(AUTHOR)
    if not current and nickname.strip():
        earlier = store.dormant_holder(board_id, nickname, tool_of(AUTHOR))
        if earlier and earlier != AUTHOR:
            _become(earlier)
            current = store.nicknames(board_id).get(AUTHOR)
            note = " (your name from before; your read position, files and promises carried over)"
    if model:
        store.set_model(board_id, AUTHOR, model)
    if current:
        store.touch(board_id, AUTHOR)
        if not note and nickname.strip() and nickname.strip().lower() != current:
            note = " (your name from before; keep it)"
        return f"You're {current} here{note}."
    nick, why = store.set_nickname(board_id, AUTHOR, nickname or None)
    if why:
        note = f" ({why}; you got this one)"
    elif not nickname.strip():
        note = " (you didn't pick a nickname; you got this one)"
    return f"You're {nick} here{note}."


def _marker(board_id: str) -> str:
    """Hooks read it from a board tool's result to link the client session to the board."""
    return f"[{board_id} you={AUTHOR}]"


def _enter(board_id: str, nickname: str = "", model: str = "", ticket: str = "") -> str:
    """Make this agent a huddler on the board for the life of this process: its name (an existing one
    wins), presence, and the marker line that links the client session's hooks. Every way onto a board
    goes through here, so no agent ends up unnamed or unlinked. Returns the marker and "You're X here"."""
    me = _identity(board_id, nickname, model, ticket)  # first: it may switch this session to its old identity
    S.joined.setdefault(board_id, time.time())
    return f"{_marker(board_id)}\n{me}"


def _board(key: str, view: bool = True):
    """The board a tool acts on: the given key, else this session's current one. The first use of a
    board in this process enters it like join_board does (an agent that kept the key across a restart
    may skip join_board), and with view=True shows it, reading on from there. Returns (key, board, preamble)."""
    k = S.use(key)
    board = store.board_for_key(k)
    if board.id in S.joined or board.status != "open":
        return k, board, ""
    head = _enter(board.id)
    if not view:
        return k, board, head + "\n"
    text, last_id = render_view(store, board)  # the plain view: a tool call never waits on summarizing
    store.set_cursor(board.id, AUTHOR, last_id)
    return k, board, f"{head} You're on the board now (no need to join_board).\n{text}\n---\n"


S = Session()


@tool
def create_board(name: str, project: str, nickname: str, goal: str = "", model: str = "") -> str:
    """Spin up board. project: repo name. nickname: cute one-word name for yourself. model: your model id."""
    existing = [b for b in store.list_boards(project) if b.name != name]
    board, key = store.create_board(name, project, AUTHOR, goal)
    S.use(key)
    store.set_cursor(board.id, AUTHOR, 0)
    out = f"{_enter(board.id, nickname, model)}\nCreated. Key: {key}\nGive user key for other sessions. Not recoverable."
    if existing:
        out += f"\nOther open boards {project}: " + ", ".join(f"{b.name} ({b.id})" for b in existing[:5])
    return out


@tool
async def join_board(key: str, nickname: str, model: str = "", status: str = "", ticket: str = "") -> str:
    """Join by key. nickname: cute one-word name for yourself (your old one if you were here before). model: your model id. status: what you'll do. ticket: from a wake message, if any.
    Returns who does what, open questions, decision log."""
    try:
        board_id = store.board_for_key(key.strip()).id
        sweep_stale_sessions(store)
        head = _enter(board_id, nickname, model, ticket)
        S.use(key)
        note = ""
        if status:
            note = _held_note(store.post(key, AUTHOR, status, kind="status"))
        text, last_id = await asyncio.to_thread(_join, store.url, key)
        store.set_cursor(board_id, AUTHOR, last_id)
        return f"{head}\n{text}{note}"
    except BoardError as e:
        return f"error: {e}"


@tool
def post(content: str, kind: str = "note", reply_to: int = 0, key: str = "") -> str:
    """kind: status|decision|question|answer|blocker|promise|result|deadend|note. reply_to: id answered/kept. Returns others' new msgs."""
    try:
        k, _, pre = _board(key)
        m = store.post(k, AUTHOR, content, kind, reply_to or None)
        new, _, _ = S.inbox(k)
    except BoardError as e:
        return f"error: {e}"
    news = "\nNew:\n" + _flag_mentions(m.board_id, new, render_entries(store, new)) if new else ""
    return (pre + f"ok #{m.id}" + _held_note(m) + _milestone_note(m) + _unknown_mentions(m.board_id, content)
            + unreachable_note(store, m.board_id, content, AUTHOR) + news + S.nudge(m.board_id))


@tool
def files(claim: list[str] | None = None, lock: list[str] | None = None, unlock: list[str] | None = None,
          release: list[str] | None = None, key: str = "") -> str:
    """Shared file inventory. claim: files/globs you're starting on. lock: claim + block others' edits (lapses if you go idle or leave). unlock: keep claim, drop lock. release: done ("*" = all). Returns who holds what."""
    try:
        _, board, pre = _board(key, view=False)
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
                holder = store.labels(board.id).get(c["agent"], c["agent"])
                lines.append(f"not {'locked' if locking else 'claimed'}: {path} held by {holder}. Ask them first.")
    except BoardError as e:
        return f"error: {e}"
    return pre + "\n".join(lines + [render_files(store, board.id) or "(no files claimed)"])


@tool
async def read_messages(wait_seconds: int = 0, since_id: int = -1, key: str = "") -> str:
    """Others' new msgs. wait_seconds<=50 blocks for one. since_id: reread from id."""
    try:
        k, _, pre = _board(key)
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
    board_id = store.board_for_key(k).id
    body = _flag_mentions(board_id, new, render_entries(store, new)) if new else "(nothing new)"
    return pre + body + f"\nlast_id={last}{tail}" + S.nudge(board_id)


def _milestone_id(x) -> int:
    try:
        return int(str(x).strip().lstrip("mM#"))
    except ValueError:
        raise BoardError(f"milestone id {x!r}? use the number after m, e.g. 7 for m7") from None


@tool
def goal(done: list[int | str] | None = None, undo: list[int | str] | None = None, add: list[str] | None = None,
         key: str = "") -> str:
    """Board's shared goal + milestones. done: milestone ids you finished. undo: reopen. add: new milestones. Returns progress."""
    try:
        k, board, pre = _board(key, view=False)
        finished, reopened = [], []
        for x in done or []:
            m = store._milestone(board.id, _milestone_id(x))
            if not m["done_at"]:
                finished.append(store.set_milestone_done(board.id, m["id"], AUTHOR)["text"])
        for x in undo or []:
            m = store._milestone(board.id, _milestone_id(x))
            if m["done_at"]:
                reopened.append(store.set_milestone_done(board.id, m["id"], AUTHOR, done=False)["text"])
        added = [store.add_milestone(board.id, t, AUTHOR)["text"] for t in add or [] if t.strip()]
        # the stream shows progress too, so others (and the user) see it without asking
        if finished:
            store.post(k, AUTHOR, "milestone done: " + "; ".join(finished), kind="result")
        if reopened:
            store.post(k, AUTHOR, "milestone reopened: " + "; ".join(reopened), kind="note")
        if added:
            store.post(k, AUTHOR, "milestone added: " + "; ".join(added), kind="note")
        board = store.get_board(board.id)
    except BoardError as e:
        return f"error: {e}"
    lines = [f"goal: {board.goal}" if board.goal else "", render_progress(store, board.id)]
    return pre + ("\n".join(ln for ln in lines if ln) or "(no goal or milestones yet)")


@tool
def close_board(outcome: str = "", key: str = "") -> str:
    """Spin down when done. Saves lessons to memory. outcome: what worked/failed."""
    try:
        k, _, _ = _board(key, view=False)
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
    sweep_stale_sessions(store, every=0)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
