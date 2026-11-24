"""The read-only board viewer: JSON API and page."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from huddlehut.viewer import Viewer, make_handler


@pytest.fixture
def served(store):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Viewer(store)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield store, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        body = r.read()
        return json.loads(body) if r.headers["Content-Type"].startswith("application/json") else body.decode()


def test_page_is_served(served):
    _, base = served
    page = get(base + "/")
    assert "<title>Huddle Hut</title>" in page and "/api/boards" in page


def test_boards_board_and_transcript(served):
    store, base = served
    board, key = store.create_board("parser", "repo-x", "claude-code-1a2b", goal="new parser")
    closed, ckey = store.create_board("old", "repo-x", "codex-3f2a")
    store.touch(board.id, "claude-code-1a2b")
    store.set_nickname(board.id, "claude-code-1a2b", "mochi")
    store.set_model(board.id, "claude-code-1a2b", "claude-opus-5-5")
    store.post(key, "claude-code-1a2b", "rewriting the lexer", "status")
    q = store.post(key, "codex-3f2a", "spans on AST nodes?", "question")
    store.post(key, "claude-code-1a2b", "yes", "answer", reply_to=q.id)
    store.post(key, "codex-3f2a", "post when seeds rebuilt", "promise")
    store.claim(board.id, "claude-code-1a2b", ["src/lexer.py"], lock=True)
    store.close_board(ckey, "codex-3f2a")

    boards = get(base + "/api/boards")
    assert [b["name"] for b in boards] == ["parser", "old"]           # open first
    assert boards[0]["active_agents"] == 1 and boards[0]["messages"] == 4

    d = get(f"{base}/api/boards/{board.id}")
    assert d["board"]["goal"] == "new parser"
    assert [m["id"] for m in d["stream"]] == sorted(m["id"] for m in d["stream"]) and len(d["stream"]) == 4
    assert d["message_count"] == 4
    agent = d["agents"]["claude-code-1a2b"]
    assert agent["name"] == "mochi" and agent["model"] == "opus 5.5"
    assert agent["files"] == [{"path": "src/lexer.py", "locked": True}]
    by_kind = {m["kind"]: m for m in d["stream"]}
    assert by_kind["promise"]["open"] is True and by_kind["question"]["open"] is False   # answered
    assert by_kind["answer"]["reply_snippet"] == "spans on AST nodes?"
    assert by_kind["status"]["name"] == "mochi" and by_kind["promise"]["name"] == "3f2a"
    assert d["waiting"] == []

    store.set_waiting(board.id, "claude-code-1a2b", "Should I rerun the measurement?")
    q2 = store.post(key, "codex-3f2a", "@user ok to drop the old seeds?", "question")
    w = get(f"{base}/api/boards/{board.id}")["waiting"]
    assert {(x["source"], x["name"]) for x in w} == {("session", "mochi"), ("board", "3f2a")}
    store.post(key, "claude-code-1a2b", "user said yes", "answer", reply_to=q2.id)
    store.set_waiting(board.id, "claude-code-1a2b", None)
    assert get(f"{base}/api/boards/{board.id}")["waiting"] == []


def test_memory_list_and_search(served):
    store, base = served
    board, key = store.create_board("ci", "repo-y", "a")
    store.add_memories(board.id, "repo-y", [
        {"title": "Pin base images", "technique": "Pin by digest.", "when_to_apply": "CI flakes after image updates",
         "tags": ["ci", "docker"]},
        {"title": "Claim files first", "technique": "Claim before editing.", "when_to_apply": "Shared checkout",
         "tags": ["coordination"]},
    ])
    assert {m["title"] for m in get(base + "/api/memory")["lessons"]} == {"Pin base images", "Claim files first"}
    hits = get(base + "/api/memory?q=docker%20flakes")["lessons"]
    assert hits[0]["title"] == "Pin base images" and hits[0]["board_name"] == "ci"


def test_unknown_board_is_404(served):
    _, base = served
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/boards/b_nope")
    assert e.value.code == 404


def post(url, headers=None):
    req = urllib.request.Request(url, data=b"", method="POST", headers=headers or {})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_end_session_frees_files_and_tells_the_board(served):
    store, base = served
    board, key = store.create_board("b", "p", "claude-code-1a2b")
    store.touch(board.id, "codex-3f2a")
    store.set_nickname(board.id, "codex-3f2a", "acorn")
    store.claim(board.id, "codex-3f2a", ["src/api.py"], lock=True)
    store.set_waiting(board.id, "codex-3f2a", "Should I rerun?")
    pr = store.post(key, "codex-3f2a", "post when seeds rebuilt", "promise")

    url = f"{base}/api/boards/{board.id}/agents/codex-3f2a/end"
    with pytest.raises(urllib.error.HTTPError) as e:          # no header: refused
        post(url)
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:          # another site: refused
        post(url, {"X-Huddlehut": "1", "Origin": "https://evil.example"})
    assert e.value.code == 403

    assert post(url, {"X-Huddlehut": "1"}) == {"ok": True}
    p = store.presence(board.id)["codex-3f2a"]
    assert p["state"] == "left" and p["waiting"] is None
    assert store.claims(board.id) == []
    last = store.all_messages(board.id)[-1]
    assert last.kind == "blocker" and last.author == "user"
    assert "acorn's session was ended by the user" in last.content and f"#{pr.id}" in last.content

    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{base}/api/boards/{board.id}/agents/nobody-0000/end", {"X-Huddlehut": "1"})
    assert e.value.code == 404


def test_close_board_from_viewer(served, monkeypatch):
    store, base = served
    started = []
    monkeypatch.setattr("huddlehut.viewer.distill_in_background", lambda url, bid: started.append(bid))
    board, key = store.create_board("stale", "p", "a")
    store.claim(board.id, "a", ["src/x.py"])
    store.set_waiting(board.id, "a", "Should I go on?")
    url = f"{base}/api/boards/{board.id}/close"
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url)
    assert e.value.code == 403
    req = urllib.request.Request(url, data=json.dumps({"outcome": "abandoned; moved to v2"}).encode(), method="POST",
                                 headers={"X-Huddlehut": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read()) == {"ok": True}
    b = store.get_board(board.id)
    assert b.status == "closed" and b.closed_by == "user" and b.outcome == "abandoned; moved to v2"
    assert store.claims(board.id) == [] and store.presence(board.id)["a"]["waiting"] is None
    assert started == [board.id]                                    # lessons saved in the background
    with pytest.raises(urllib.error.HTTPError) as e:                # already closed
        post(url, {"X-Huddlehut": "1"})
    assert e.value.code == 404


def test_sessions_idle_for_days_are_ended(store):
    import time
    board, key = store.create_board("b", "p", "a")
    store.touch(board.id, "old-agent")
    store.touch(board.id, "fresh-agent")
    store.db.execute("UPDATE presence SET seen_at=? WHERE agent='old-agent'", (time.time() - 4 * 86400,))
    assert store.end_stale_sessions() == [(board.id, "old-agent")]
    p = store.presence(board.id)
    assert p["old-agent"]["state"] == "left" and p["fresh-agent"]["state"] == "active"
    assert "timed out after 3 days without activity" in store.all_messages(board.id)[-1].content


def test_answering_an_ask_stops_it_waiting_on_the_user(store, monkeypatch):
    from huddlehut import wake_service
    monkeypatch.setattr(wake_service, "ensure", lambda: None)
    board, key = store.create_board("b", "p", "codex-aaaa")
    store.set_nickname(board.id, "codex-aaaa", "lark")
    store.set_nickname(board.id, "codex-bbbb", "pippin")
    a = store.post(key, "codex-aaaa", "@user run Gate A now?", "question")
    b = store.post(key, "codex-bbbb", "@user confirm Gate A?", "question")
    v = Viewer(store)
    assert {w["id"] for w in v.board(board.id)["waiting"]} == {a.id, b.id}

    v.post(board.id, "yes, run it", reply_to=a.id)          # answered here
    v.post(board.id, "done: answered elsewhere", reply_to=b.id)   # answered through another agent
    assert v.board(board.id)["waiting"] == []
    msgs = store.all_messages(board.id)
    assert msgs[-2].content == "@lark yes, run it" and msgs[-2].reply_to == a.id and msgs[-2].kind == "answer"
    assert msgs[-1].content.startswith("@pippin ")
    with pytest.raises(Exception):
        v.post(board.id, "hi", reply_to=9999)


def test_user_at_all_answers_every_open_question(store, monkeypatch):
    from huddlehut import wake_service
    monkeypatch.setattr(wake_service, "ensure", lambda: None)
    board, key = store.create_board("b", "p", "codex-aaaa")
    store.set_nickname(board.id, "codex-aaaa", "lark")
    q1 = store.post(key, "codex-aaaa", "@user run Gate A?", "question")
    q2 = store.post(key, "codex-bbbb", "@lark which packet?", "question")   # agent to agent counts too
    bl = store.post(key, "codex-bbbb", "browser down", "blocker")
    v = Viewer(store)

    v.post(board.id, "status please")              # no typed @all: answers nothing
    assert {m.id for m in store.open_items(board.id)} == {q1.id, q2.id, bl.id}

    v.post(board.id, "@all all confirmations are go")
    assert {m.id for m in store.open_items(board.id)} == {bl.id}   # blockers close by the work
    assert v.board(board.id)["waiting"] == []
    later = store.post(key, "codex-aaaa", "@user one more?", "question")
    assert later.id in {m.id for m in store.open_items(board.id)}  # only questions asked before it
