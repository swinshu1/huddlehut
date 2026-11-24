"""An agent keeps one identity per board: a restarted or resumed session comes back with its old name,
read position and files, and every agent has to give a nickname."""

import re
import time

import anyio
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from huddlehut.store import IDLE_AFTER, Store, tool_of
from test_mcp import _call, _params


def test_identity_is_remembered_per_client(store):
    assert store.identity("claude-code:s1", "claude-code-aaaa") == "claude-code-aaaa"
    assert store.identity("claude-code:s1", "claude-code-bbbb") == "claude-code-aaaa"   # first one sticks
    store.remember_identity("claude-code:s1", "claude-code-cccc")
    assert store.identity("claude-code:s1", "claude-code-dddd") == "claude-code-cccc"
    assert tool_of("claude-code-3f2a") == "claude-code" and tool_of("codex") == "codex"


def test_only_an_idle_or_departed_holder_of_the_same_tool_is_reclaimed(store):
    board, _ = store.create_board("b", "p", "codex-aaaa")
    store.set_nickname(board.id, "codex-aaaa", "acorn")
    store.set_nickname(board.id, "claude-code-bbbb", "mochi")
    assert store.dormant_holder(board.id, "acorn", "codex") is None             # still active: someone else
    store.mark_left(board.id, "codex-aaaa")
    assert store.dormant_holder(board.id, "Acorn", "codex") == "codex-aaaa"
    assert store.dormant_holder(board.id, "acorn", "claude-code") is None       # another tool: someone else
    store.db.execute("UPDATE presence SET seen_at=? WHERE agent='claude-code-bbbb'", (time.time() - IDLE_AFTER - 5,))
    assert store.dormant_holder(board.id, "mochi", "claude-code") == "claude-code-bbbb"   # idle counts too


def _marker_agent(text):
    return re.search(r"you=([\w.-]+)\]", text).group(1)


def test_restarted_claude_session_is_the_same_agent(db_url):
    """Claude Code passes its session id to the board server; a new server for that session (resume,
    reload, plugin update) gets the same agent id back."""

    async def run():
        async with stdio_client(_params(db_url, "claude-code", CLAUDE_CODE_SESSION_ID="sess-1")) as (r, w), \
                   ClientSession(r, w) as first:
            await first.initialize()
            created = await _call(first, "create_board", name="b", project="p", nickname="mochi")
            key = re.search(r"brd_\S+", created).group(0)
            me = _marker_agent(created)
            await _call(first, "files", claim=["src/lexer.py"])
        # someone posts while it's down
        Store(db_url).post(key, "codex-cccc", "lexer spans?", "question")

        async with stdio_client(_params(db_url, "claude-code", CLAUDE_CODE_SESSION_ID="sess-1")) as (r, w), \
                   ClientSession(r, w) as again:
            await again.initialize()
            assert (await _call(again, "post", content="hi")).startswith("error: no board. join_board(key, nickname)")
            back = await _call(again, "join_board", key=key, nickname="pebble")
            assert _marker_agent(back) == me
            assert "You're mochi here (your name from before; keep it)." in back
            assert "Files:\nmochi: src/lexer.py" in back
            assert (await _call(again, "read_messages")).startswith("(nothing new)")   # join view covered it

        # a different session is a different agent
        async with stdio_client(_params(db_url, "claude-code", CLAUDE_CODE_SESSION_ID="sess-2")) as (r, w), \
                   ClientSession(r, w) as other:
            await other.initialize()
            joined = await _call(other, "join_board", key=key, nickname="mochi")
            assert _marker_agent(joined) != me and "'mochi' is taken" in joined

    anyio.run(run)


def test_codex_gets_its_identity_back_by_nickname(db_url):
    """Codex passes no session id, so a Codex agent rejoining under the name it had, after that identity
    went idle or left, picks it back up."""

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as first:
            await first.initialize()
            created = await _call(first, "create_board", name="b", project="p", nickname="acorn")
            key = re.search(r"brd_\S+", created).group(0)
            old = _marker_agent(created)
            await _call(first, "post", content="post when seeds rebuilt", kind="promise")

        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as again:
            await again.initialize()
            # the old one still looks active: an agent picking the same cute name is someone else
            taken = await _call(again, "join_board", key=key, nickname="acorn")
            assert "'acorn' is taken" in taken and _marker_agent(taken) != old

        store = Store(db_url)
        board = store.board_for_key(key)
        store.mark_left(board.id, old)                       # its session ended (or it went idle)
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as later:
            await later.initialize()
            back = await _call(later, "join_board", key=key, nickname="acorn", model="gpt-5-codex")
            assert _marker_agent(back) == old
            assert "You're acorn here (your name from before; your read position, files and promises carried over)." in back
            assert await _call(later, "post", content="seeds rebuilt", kind="result", reply_to=1) == "ok #2"
        assert store.presence(board.id)[old]["state"] == "active"
        assert store.presence(board.id)[old]["model"] == "gpt-5-codex"

    anyio.run(run)


def test_nickname_is_required(db_url):
    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert "nickname" in tools["join_board"].input_schema["required"]
            assert "nickname" in tools["create_board"].input_schema["required"]
            # an empty one still gets the agent a name, and says so
            created = await _call(s, "create_board", name="b", project="p", nickname="")
            assert re.search(r"You're [a-z-]+ here \(you didn't pick a nickname; you got this one\)\.", created)

    anyio.run(run)


def test_wake_ticket_brings_back_an_identity_that_is_still_active(db_url):
    """A Codex thread resumed by the app-server daemon gets a fresh board server. The wake message's ticket
    gets it the agent's identity back, even moments after that agent was active (when a nickname alone
    would be refused as someone else)."""

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as first:
            await first.initialize()
            created = await _call(first, "create_board", name="b", project="p", nickname="acorn")
            key = re.search(r"brd_\S+", created).group(0)
            old = _marker_agent(created)
        store = Store(db_url)
        ticket = store.issue_ticket(store.board_for_key(key).id, old)

        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as woken:
            await woken.initialize()
            back = await _call(woken, "join_board", key=key, nickname="acorn", ticket=ticket)
            assert _marker_agent(back) == old
            assert "You're acorn here (your name from before; your read position, files and promises carried over)." in back

        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as later:
            await later.initialize()
            again = await _call(later, "join_board", key=key, nickname="acorn", ticket=ticket)   # spent
            assert _marker_agent(again) != old and "'acorn' is taken" in again

    anyio.run(run)


def test_departed_name_goes_to_the_next_session_that_asks_for_it(db_url):
    """Each Codex thread (and each Claude Code session) is its own client. When the session holding a
    name has left, a new session asking for that name becomes that agent: same name, read position,
    files and promises. The old session, if it ever returns, is a new agent rather than a second copy."""

    async def run():
        async with stdio_client(_params(db_url, "codex", HUDDLEHUT_SESSION="thread-1")) as (r, w), \
                   ClientSession(r, w) as first:
            await first.initialize()
            created = await _call(first, "create_board", name="b", project="p", nickname="acorn")
            key = re.search(r"brd_\S+", created).group(0)
            old = _marker_agent(created)
            await _call(first, "post", content="post when seeds rebuilt", kind="promise")

        store = Store(db_url)
        board = store.board_for_key(key)
        # only idle, not left: its own thread may still wake, so another thread doesn't get it
        store.db.execute("UPDATE presence SET seen_at=? WHERE agent=?", (time.time() - IDLE_AFTER - 5, old))
        async with stdio_client(_params(db_url, "codex", HUDDLEHUT_SESSION="thread-2")) as (r, w), \
                   ClientSession(r, w) as other:
            await other.initialize()
            taken = await _call(other, "join_board", key=key, nickname="acorn")
            assert "'acorn' is taken" in taken and _marker_agent(taken) != old

        store.mark_left(board.id, old)
        async with stdio_client(_params(db_url, "codex", HUDDLEHUT_SESSION="thread-3")) as (r, w), \
                   ClientSession(r, w) as later:
            await later.initialize()
            back = await _call(later, "join_board", key=key, nickname="acorn")
            assert _marker_agent(back) == old
            assert "You're acorn here (your name from before; your read position, files and promises carried over)." in back
            assert await _call(later, "post", content="seeds rebuilt", kind="result", reply_to=1) == "ok #2"
        assert store.owner_of(old) == "codex:thread-3"

        async with stdio_client(_params(db_url, "codex", HUDDLEHUT_SESSION="thread-1")) as (r, w), \
                   ClientSession(r, w) as returned:
            await returned.initialize()
            again = await _call(returned, "join_board", key=key, nickname="oak")
            assert _marker_agent(again) != old and "You're oak here" in again

    anyio.run(run)


def test_departed_agents_drop_out_of_huddlers_and_now(store):
    from huddlehut.compact import render_view
    board, key = store.create_board("b", "p", "codex-aaaa")
    store.set_nickname(board.id, "codex-aaaa", "acorn")
    store.set_nickname(board.id, "codex-bbbb", "moss")
    store.post(key, "codex-aaaa", "building fixtures", kind="status")
    store.post(key, "codex-bbbb", "reviewing packet", kind="status")
    store.mark_left(board.id, "codex-bbbb")
    text, _ = render_view(store, board)
    assert "acorn" in text and "building fixtures" in text
    assert "moss" not in text and "reviewing packet" not in text
