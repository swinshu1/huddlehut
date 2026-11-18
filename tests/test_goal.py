"""The board's collective goal: a goal plus milestones the user sets and agents check off."""

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from huddlehut import compact as C
from huddlehut.store import BoardError
from huddlehut.viewer import Viewer, make_handler
from test_mcp import _call, _params


def test_milestones_in_order_checked_off_and_removed(store):
    board, key = store.create_board("b", "p", "a", goal="ship v1")
    other, _ = store.create_board("c", "p", "a")
    m1 = store.add_milestone(board.id, "  lexer   spans ", "user")
    m2 = store.add_milestone(board.id, "parser tests", "a")
    assert [m["text"] for m in store.milestones(board.id)] == ["lexer spans", "parser tests"]
    with pytest.raises(BoardError):
        store.add_milestone(board.id, "   ", "a")
    with pytest.raises(BoardError, match="no milestone"):        # ids are per board
        store.set_milestone_done(other.id, m1["id"], "a")

    done = store.set_milestone_done(board.id, m1["id"], "codex-1a2b")
    assert done["done_by"] == "codex-1a2b" and done["done_at"]
    again = store.set_milestone_done(board.id, m1["id"], "someone-else")
    assert again["done_by"] == "codex-1a2b"                       # first finisher keeps the credit
    assert store.set_milestone_done(board.id, m1["id"], "a", done=False)["done_at"] is None

    store.delete_milestone(board.id, m2["id"])
    assert [m["id"] for m in store.milestones(board.id)] == [m1["id"]]
    assert store.set_goal(board.id, " ship\n v2 ").goal == "ship v2"


def test_join_view_shows_progress_and_whats_left(store):
    board, key = store.create_board("b", "p", "a", goal="ship v1")
    assert "Progress" not in C.render_view(store, board)[0]
    m1 = store.add_milestone(board.id, "lexer spans", "user")
    m2 = store.add_milestone(board.id, "parser tests", "user")
    store.set_milestone_done(board.id, m1["id"], "a")
    view = C.render_view(store, store.get_board(board.id))[0]
    assert view.split("\n")[:2] == ["b [p] goal: ship v1", f"Progress 1/2, todo: m{m2['id']} parser tests"]
    store.set_milestone_done(board.id, m2["id"], "a")
    assert "Progress 2/2, all milestones done" in C.render_view(store, board)[0]


def test_agents_check_off_milestones_with_the_goal_tool(db_url, tmp_path):
    async def run():
        async with stdio_client(_params(db_url, "claude-code")) as (r1, w1), ClientSession(r1, w1) as claude, \
                   stdio_client(_params(db_url, "codex")) as (r2, w2), ClientSession(r2, w2) as codex:
            await claude.initialize()
            await codex.initialize()
            created = await _call(claude, "create_board", name="b", project="p", goal="ship v1", nickname="mochi")
            key = re.search(r"brd_\S+", created).group(0)
            await _call(codex, "join_board", key=key, nickname="acorn")

            out = await _call(claude, "goal", add=["lexer spans", "parser tests"])
            ids = [int(i) for i in re.findall(r"m(\d+)", out)]
            assert out == f"goal: ship v1\nProgress 0/2, todo: m{ids[0]} lexer spans; m{ids[1]} parser tests"

            # "m7" and 7 both work; finishing posts a result everyone sees
            out = await _call(codex, "goal", done=[f"m{ids[0]}"])
            assert out == f"goal: ship v1\nProgress 1/2, todo: m{ids[1]} parser tests"
            news = await _call(claude, "read_messages")
            assert "acorn/result: milestone done: lexer spans" in news

            assert (await _call(codex, "goal", done=[999])).startswith("error: no milestone 999")
            assert (await _call(codex, "goal", done=["lexer"])).startswith("error: milestone id 'lexer'?")
            assert "Progress 0/2" in await _call(codex, "goal", undo=[ids[0]])
            assert "Progress 0/2" in await _call(codex, "goal")    # no args: just shows it

    anyio.run(run)


@pytest.fixture
def served(store):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Viewer(store)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield store, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def send(url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-Huddlehut": "1"} if headers is None else headers)})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def board_json(base, board_id):
    with urllib.request.urlopen(f"{base}/api/boards/{board_id}") as r:
        return json.loads(r.read())


def test_user_sets_goal_and_milestones_from_the_viewer(served):
    store, base = served
    board, key = store.create_board("b", "p", "claude-code-1a2b")
    store.set_nickname(board.id, "claude-code-1a2b", "mochi")
    url = f"{base}/api/boards/{board.id}"

    with pytest.raises(urllib.error.HTTPError) as e:              # no header: refused
        send(url + "/goal", {"goal": "x"}, headers={})
    assert e.value.code == 403

    assert send(url + "/goal", {"goal": "ship   the parser"}) == {"ok": True}
    last = store.all_messages(board.id)[-1]
    assert (last.author, last.kind, last.content) == ("user", "decision", "@all new goal: ship the parser")
    send(url + "/goal", {"goal": "ship the parser"})              # unchanged: nothing posted
    assert store.all_messages(board.id)[-1].id == last.id

    send(url + "/milestones", {"text": "lexer spans"})
    send(url + "/milestones", {"text": "parser tests"})
    ms = board_json(base, board.id)["milestones"]
    assert [m["text"] for m in ms] == ["lexer spans", "parser tests"] and not any(m["done"] for m in ms)
    assert store.all_messages(board.id)[-1].content == "milestone added: parser tests"

    store.set_milestone_done(board.id, ms[0]["id"], "claude-code-1a2b")   # an agent finished one
    send(f"{url}/milestones/{ms[1]['id']}", {"done": True})                # the user ticks the other
    d = board_json(base, board.id)
    assert [(m["done"], m["done_by_name"]) for m in d["milestones"]] == [(True, "mochi"), (True, "user")]
    assert d["board"]["goal"] == "ship the parser"
    assert store.all_messages(board.id)[-1].kind == "result"

    send(f"{url}/milestones/{ms[1]['id']}", {"delete": True})
    assert [m["text"] for m in board_json(base, board.id)["milestones"]] == ["lexer spans"]
    with pytest.raises(urllib.error.HTTPError) as e:
        send(f"{url}/milestones/{ms[1]['id']}", {"done": True})
    assert e.value.code == 404

    store.close_board(key, "user")
    with pytest.raises(urllib.error.HTTPError) as e:              # closed boards are read-only
        send(url + "/goal", {"goal": "new"})
    assert e.value.code == 404


def test_a_result_reminds_the_agent_of_open_milestones(db_url):
    """Agents finish milestones without checking them off; a result post lists what's open, at the moment
    a milestone is most likely done."""

    async def run():
        async with stdio_client(_params(db_url, "codex")) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            created = await _call(s, "create_board", name="b", project="p", goal="ship v1", nickname="acorn")
            out = await _call(s, "goal", add=["phase 0", "eval run"])
            ids = [int(i) for i in re.findall(r"m(\d+)", out)]
            note = await _call(s, "post", content="phase 0 built, tests pass", kind="result")
            chat = await _call(s, "post", content="starting the eval", kind="status")
            await _call(s, "goal", done=[ids[0]])
            later = await _call(s, "post", content="eval half done", kind="result")
            await _call(s, "goal", done=[ids[1]])
            last = await _call(s, "post", content="eval done", kind="result")
            return created, ids, note, chat, later, last

    created, ids, note, chat, later, last = anyio.run(run)
    assert f"Open milestones: m{ids[0]} phase 0; m{ids[1]} eval run. If this result finished one, goal(done=[id])." in note
    assert "Open milestones" not in chat                          # only results
    assert f"Open milestones: m{ids[1]} eval run." in later       # done ones drop off
    assert "Open milestones" not in last                          # nothing left
