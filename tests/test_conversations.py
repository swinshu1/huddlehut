"""One board server can serve many conversations: the Codex app runs one for all its threads and names the
thread on every call. Each thread is its own agent, with its own current board, and keeps its identity
across server restarts; a subagent never becomes its parent."""

import re
import time

import anyio
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from huddlehut.store import IDLE_AFTER, Store
from test_mcp import _params

MARKER = re.compile(r"\[(b_[0-9a-f]{8}) you=([\w.-]+)\]")


def codex_meta(thread, model="gpt-6-sol", parent=None):
    """What the Codex app sends with each tool call (see a real one in swinshu1/huddlehut#50)."""
    turn = {"thread_id": thread, "session_id": thread, "turn_id": f"{thread}-t1", "model": model,
            "thread_source": "subagent" if parent else "user"}
    meta = {"threadId": thread, "sessionId": thread, "x-codex-turn-metadata": turn}
    if parent:
        meta["x-codex-parent-thread-id"] = parent
    return meta


async def call(session, name, meta=None, **args):
    result = await session.call_tool(name, args, meta=meta)
    return "".join(c.text for c in result.content if getattr(c, "text", None))


def test_threads_on_one_server_are_separate_agents_with_their_own_board(db_url):
    store = Store(db_url)
    muse, muse_key = store.create_board("muse", "p", "claude-code-aaaa")
    review, review_key = store.create_board("review", "p", "claude-code-aaaa")

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            otter = await call(s, "join_board", codex_meta("T-otter"), key=muse_key, nickname="otter")
            both = await call(s, "join_board", codex_meta("T-sprout"), key=muse_key, nickname="sprout")
            sprout = await call(s, "join_board", codex_meta("T-sprout", model="gpt-6.2-astra"),
                                key=review_key, nickname="sprout")      # its current board now
            # no key: each thread's post goes to its own current board
            a = await call(s, "post", codex_meta("T-otter"), content="harness ready", kind="status")
            b = await call(s, "post", codex_meta("T-sprout"), content="phase 0 done", kind="status")
            return otter, sprout, both, a, b

    otter, sprout, both, a, b = anyio.run(run)
    (_, o), (_, sp), (_, sp2) = MARKER.search(otter).groups(), MARKER.search(sprout).groups(), MARKER.search(both).groups()
    assert o != sp and sp == sp2                                          # two agents, one per thread
    assert "You're sprout here." in both                                  # same board as otter, still apart
    assert store.presence(muse.id)[o]["model"] == "gpt-6-sol"            # what Codex says the model is
    assert store.presence(review.id)[sp]["model"] == "gpt-6.2-astra"
    assert [(m.author, m.content) for m in store.all_messages(review.id)] == [(sp, "phase 0 done")]
    assert [(m.author, m.content) for m in store.all_messages(muse.id)] == [(o, "harness ready")]


def test_a_thread_keeps_its_identity_across_server_restarts(db_url):
    store = Store(db_url)
    board, key = store.create_board("muse", "p", "claude-code-aaaa")

    async def session(meta, name, **args):
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return await call(s, name, meta, **args)

    async def run():
        first = await session(codex_meta("T-otter"), "join_board", key=key, nickname="otter")
        store.post(key, "claude-code-aaaa", "@otter seeds?", "question")
        back = await session(codex_meta("T-otter"), "post", key=key, content="seeds rebuilt", kind="answer")
        return first, back

    first, back = anyio.run(run)
    me = MARKER.search(first).group(2)
    assert MARKER.search(back).group(2) == me and "You're otter here." in back   # no rejoin, no new name
    assert store.all_messages(board.id)[-1].author == me


def test_a_subagent_never_becomes_its_parent(db_url):
    store = Store(db_url)
    board, key = store.create_board("muse", "p", "claude-code-aaaa")

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            parent = await call(s, "join_board", codex_meta("T-otter"), key=key, nickname="otter")
            me = MARKER.search(parent).group(2)
            # the parent goes idle; its subagent (a forked thread that remembers being "otter") joins
            store.db.execute("UPDATE presence SET seen_at=? WHERE agent=?", (time.time() - IDLE_AFTER - 5, me))
            sub = await call(s, "join_board", codex_meta("T-sub", parent="T-otter"), key=key, nickname="otter")
            ticket = store.issue_ticket(board.id, me)                    # not even with a ticket
            again = await call(s, "join_board", codex_meta("T-sub", parent="T-otter"), key=key, nickname="otter",
                               ticket=ticket)
            return me, sub, again

    me, sub, again = anyio.run(run)
    assert MARKER.search(sub).group(2) != me and "'otter' is taken" in sub
    assert MARKER.search(again).group(2) != me
    assert store.nicknames(board.id)[me] == "otter"
