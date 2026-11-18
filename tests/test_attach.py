"""Any board use puts an agent on the board, not just join_board: an agent that kept the key (say, across a
restart) and goes straight to posting still gets its name, a hook link and the board view, once."""

import re

import anyio
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from huddlehut import hook
from huddlehut.store import Store
from test_mcp import _call, _params

MARKER = re.compile(r"\[(b_[0-9a-f]{8}) you=([\w.-]+)\]")


def _board(db_url):
    store = Store(db_url)
    board, key = store.create_board("parser", "repo", "claude-code-aaaa", goal="ship the parser")
    store.set_nickname(board.id, "claude-code-aaaa", "mochi")
    store.post(key, "claude-code-aaaa", "lexer done", "result")
    return store, board, key


def test_posting_without_joining_names_links_and_shows_the_board(db_url, tmp_path, monkeypatch):
    store, board, key = _board(db_url)

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            first = await _call(s, "post", content="taking the parser", kind="status", key=key)
            again = await _call(s, "post", content="parser half done", kind="status")
            return first, again

    first, again = anyio.run(run)
    board_id, me = MARKER.search(first).groups()
    assert board_id == board.id
    nick = store.nicknames(board.id)[me]
    assert f"You're {nick} here (you didn't pick a nickname; you got this one)." in first
    assert "ship the parser" in first and "lexer done" in first          # it sees the board, once
    assert re.search(r"\nok #\d+$", first)
    assert re.fullmatch(r"ok #\d+", again)                               # already on it: just the result

    # the marker in a post result links the client session's hooks, as join_board's does
    monkeypatch.setattr(hook, "SESSIONS", tmp_path / "sessions")
    hook.post_tool({"session_id": "S9", "tool_name": "mcp__huddlehut__post",
                    "tool_response": [{"type": "text", "text": first}]}, "codex")
    assert (hook.load("S9")["board"], hook.load("S9")["me"]) == (board.id, me)


def test_restarted_session_that_skips_join_keeps_its_name(db_url):
    store, board, key = _board(db_url)

    async def session(**env):
        async with stdio_client(_params(db_url, "claude-code", **env)) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return await _call(s, "read_messages", key=key)

    async def run():
        joined = None
        async with stdio_client(_params(db_url, "claude-code", CLAUDE_CODE_SESSION_ID="sess-7")) as (r, w), \
                   ClientSession(r, w) as s:
            await s.initialize()
            joined = await _call(s, "join_board", key=key, nickname="pebble")
        store.post(key, "claude-code-aaaa", "@pebble seeds rebuilt?", "question")
        back = await session(CLAUDE_CODE_SESSION_ID="sess-7")
        return joined, back

    joined, back = anyio.run(run)
    assert MARKER.search(back).group(2) == MARKER.search(joined).group(2)
    assert "You're pebble here." in back and "seeds rebuilt?" in back
    assert back.endswith("(nothing new)\nlast_id=" + str(store.max_message_id(board.id)))   # not shown twice


def test_files_and_goal_enter_without_the_full_view(db_url):
    store, board, key = _board(db_url)

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            claimed = await _call(s, "files", claim=["src/parser.py"], key=key)
            progress = await _call(s, "goal")
            return claimed, progress

    claimed, progress = anyio.run(run)
    me = MARKER.search(claimed).group(2)
    assert store.nicknames(board.id).get(me) and "lexer done" not in claimed
    assert "src/parser.py" in claimed
    assert progress.startswith("goal: ship the parser") and not MARKER.search(progress)   # entered once
    assert [c["agent"] for c in store.claims(board.id)] == [me]
