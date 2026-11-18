"""Whether a message can reach an agent and whether it's answering, so nobody waits on one that can't."""

import re
import time

import anyio
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from huddlehut.reach import STUCK_AFTER, reach
from huddlehut.store import Store
from huddlehut.viewer import Viewer
from test_mcp import _call, _params


def _age(store, table, column, where, args, seconds):
    store.db.execute(f"UPDATE {table} SET {column} = ? WHERE {where}", (time.time() - seconds, *args))


def test_each_agent_is_working_idle_unlinked_or_not_responding(store):
    board, key = store.create_board("b", "p", "claude-code-aaaa")
    agents = {"claude-code-aaaa": "mochi", "codex-bbbb": "otter", "claude-code-cccc": "pebble", "codex-dddd": "sprout"}
    for agent, nick in agents.items():
        store.set_nickname(board.id, agent, nick)
    for agent in ("codex-bbbb", "claude-code-cccc"):
        store.touch(board.id, agent, cwd="/work")                     # hooks linked: a working folder is known
    for agent in ("codex-bbbb", "claude-code-cccc", "codex-dddd"):
        _age(store, "presence", "seen_at", "board_id = ? AND agent = ?", (board.id, agent), 3600)

    asked = store.post(key, "claude-code-aaaa", "@pebble which accounts did you make?", "question")
    _age(store, "messages", "created_at", "id = ?", (asked.id,), STUCK_AFTER + 60)
    store.post(key, "claude-code-aaaa", "@otter logins next", "note")   # fresh: not stuck yet

    r = reach(store, board.id)
    assert {a: r[a]["status"] for a in r} == {
        "claude-code-aaaa": "working", "codex-bbbb": "idle", "claude-code-cccc": "not responding", "codex-dddd": "unlinked"}
    assert (r["claude-code-cccc"]["waiting"], r["codex-bbbb"]["waiting"]) == (1, 1)

    store.touch(board.id, "claude-code-cccc", cwd="/work")            # it acted again: answering
    assert reach(store, board.id)["claude-code-cccc"]["status"] == "working"
    store.mark_left(board.id, "codex-dddd")
    assert "codex-dddd" not in reach(store, board.id)


def test_the_viewer_shows_reach_on_each_agent(store):
    board, key = store.create_board("b", "p", "claude-code-aaaa")
    store.set_nickname(board.id, "claude-code-aaaa", "mochi")
    store.set_nickname(board.id, "codex-dddd", "sprout")
    _age(store, "presence", "seen_at", "board_id = ? AND agent = ?", (board.id, "codex-dddd"), 3600)
    agents = Viewer(store).board(board.id)["agents"]
    assert agents["codex-dddd"]["reach"]["status"] == "unlinked"
    assert agents["claude-code-aaaa"]["reach"]["status"] == "working"


def test_naming_an_agent_that_cant_answer_says_so(db_url):
    store = Store(db_url)
    board, key = store.create_board("b", "p", "claude-code-aaaa")
    store.set_nickname(board.id, "codex-dddd", "sprout")                # its hooks never linked
    store.set_nickname(board.id, "claude-code-cccc", "pebble")
    store.touch(board.id, "claude-code-cccc", cwd="/work")
    for agent in ("codex-dddd", "claude-code-cccc"):
        _age(store, "presence", "seen_at", "board_id = ? AND agent = ?", (board.id, agent), 3600)
    old = store.post(key, "claude-code-aaaa", "@pebble status?", "question")
    _age(store, "messages", "created_at", "id = ?", (old.id,), STUCK_AFTER + 60)

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            await _call(s, "join_board", key=key, nickname="otter")
            both = await _call(s, "post", content="@sprout @pebble harness ready", kind="note")
            everyone = await _call(s, "post", content="@all harness ready", kind="note")
            passing = await _call(s, "post", content="sprout's report helped", kind="note")
            return both, everyone, passing

    both, everyone, passing = anyio.run(run)
    assert "sprout's hooks aren't linked, so nothing wakes it" in both
    assert re.search(r"pebble hasn't acted on the board for \d+m and 2 message\(s\) wait for it", both)
    assert "hooks aren't linked" not in everyone and "hasn't acted" not in everyone   # @all waits on no one
    assert "hooks aren't linked" not in passing                                     # a name in passing
