"""The user picks a name that shows instead of "user", for people and agents alike."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from huddlehut import cli
from huddlehut.compact import render_view
from huddlehut.store import BoardError
from huddlehut.viewer import Viewer, make_handler


def test_name_rules(store):
    assert store.user_name() == "user"
    assert store.set_user_name("  Michelle ") == "michelle"
    assert store.user_name() == "michelle"
    for bad in ("all", "x", "1abc", "codex"):
        with pytest.raises(BoardError, match="isn't usable"):
            store.set_user_name(bad)
    board, _ = store.create_board("b", "p", "claude-code-1a2b")
    store.set_nickname(board.id, "claude-code-1a2b", "mochi")
    with pytest.raises(BoardError, match="an agent on b is called mochi"):
        store.set_user_name("mochi")
    nick, why = store.set_nickname(board.id, "codex-3f2a", "michelle")     # agents can't take it
    assert nick != "michelle" and why == "'michelle' is taken"
    assert store.set_user_name("") == "user"                              # resets


def test_name_shows_everywhere_including_old_posts(store):
    board, key = store.create_board("b", "p", "claude-code-1a2b")
    store.set_nickname(board.id, "claude-code-1a2b", "mochi")
    store.post(key, "user", "@all new goal: ship it", "decision")
    store.set_user_name("michelle")
    assert store.labels(board.id)["user"] == "michelle"
    view = render_view(store, store.get_board(board.id))[0]
    assert "User: michelle (@michelle or @user)" in view
    assert "michelle (user)/decision: @all new goal: ship it" in view


@pytest.fixture
def served(store):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Viewer(store)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield store, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _req(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_viewer_sets_the_name_and_pins_posts_addressed_to_it(served):
    store, base = served
    board, key = store.create_board("b", "p", "codex-3f2a")
    assert _req(base + "/api/me") == {"name": "user"}
    with pytest.raises(urllib.error.HTTPError) as e:                    # no header: refused
        _req(base + "/api/me", {"name": "michelle"})
    assert e.value.code == 403
    assert _req(base + "/api/me", {"name": "Michelle"}, {"X-Huddlehut": "1"}) == {"name": "michelle"}
    with pytest.raises(urllib.error.HTTPError) as e:
        _req(base + "/api/me", {"name": "all"}, {"X-Huddlehut": "1"})
    assert "isn't usable" in json.loads(e.value.read())["error"]

    q = store.post(key, "codex-3f2a", "@michelle keep the old parser for a release?", "question")
    d = _req(f"{base}/api/boards/{board.id}")
    assert [w["id"] for w in d["waiting"]] == [q.id]                   # @name counts as asking you
    store.post(key, "user", "keep it", "answer", reply_to=q.id)
    d = _req(f"{base}/api/boards/{board.id}")
    assert d["waiting"] == [] and d["stream"][-1]["name"] == "michelle"


def test_cli_sets_the_name(db_url, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HUDDLEHUT_DATABASE_URL", db_url)
    monkeypatch.setenv("HUDDLEHUT_HOME", str(tmp_path))
    assert cli.main(["config", "--name", "michelle"]) == 0
    assert "You're michelle on the boards." in capsys.readouterr().out
    assert cli.main(["config", "--name", "all"]) == 1


def test_user_pings_agents_from_the_viewer(served):
    """A ping is a post from the user that mentions agents; the hooks' wake logic treats it like any mention."""
    from huddlehut.hook import _needs_me

    store, base = served
    board, key = store.create_board("b", "p", "claude-code-1a2b")
    store.set_nickname(board.id, "claude-code-1a2b", "mochi")
    store.set_nickname(board.id, "codex-3f2a", "acorn")
    url = f"{base}/api/boards/{board.id}/post"
    with pytest.raises(urllib.error.HTTPError) as e:                    # no header: refused
        _req(url, {"content": "@mochi"})
    assert e.value.code == 403
    h = {"X-Huddlehut": "1"}

    _req(url, {"content": "@mochi"}, h)                                   # a bare ping gets a nudge
    _req(url, {"content": "@all, ship it today?"}, h)
    _req(url, {"content": "thanks all"}, h)                               # names nobody: it's for everyone
    _req(url, {"content": "@3f2a rebase on main"}, h)                     # a tag names an agent too
    _req(url, {"content": "@bob owns the parser"}, h)                     # nobody here is bob
    msgs = store.all_messages(board.id)
    assert [(m.author, m.kind, m.content) for m in msgs] == [
        ("user", "note", "@mochi take a look at the board"),
        ("user", "question", "@all, ship it today?"),
        ("user", "note", "@all thanks all"),
        ("user", "note", "@3f2a rebase on main"),
        ("user", "note", "@all @bob owns the parser"),
    ]
    woken = lambda agent: [m.id for m in _needs_me(store, board.id, agent, msgs)]  # noqa: E731
    assert woken("claude-code-1a2b") == [msgs[0].id, msgs[1].id, msgs[2].id, msgs[4].id]
    assert woken("codex-3f2a") == [msgs[1].id, msgs[2].id, msgs[3].id, msgs[4].id]

    store.set_user_name("juniper")                                        # agents see who it's from
    from huddlehut.compact import render_entries
    assert render_entries(store, msgs[:1]) == f"#{msgs[0].id} juniper (user): @mochi take a look at the board"

    with pytest.raises(urllib.error.HTTPError) as e:
        _req(url, {"content": "   "}, h)
    assert json.loads(e.value.read())["error"] == "say something first"
    store.close_board(key, "user")
    with pytest.raises(urllib.error.HTTPError) as e:
        _req(url, {"content": "@mochi"}, h)
    assert "closed" in json.loads(e.value.read())["error"]
