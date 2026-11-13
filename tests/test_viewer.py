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
    assert [m["content"] for m in d["now"]] == ["rewriting the lexer"]
    assert [m["kind"] for m in d["open"]] == ["promise"]               # the answered question isn't open
    assert d["files"] == [{"agent": "claude-code-1a2b", "name": "mochi", "path": "src/lexer.py", "locked": True}]
    assert d["agents"]["claude-code-1a2b"]["name"] == "mochi" and d["agents"]["claude-code-1a2b"]["model"] == "opus 5.5"
    assert d["now"][0]["name"] == "mochi" and d["open"][0]["name"] == "3f2a"   # no nickname: the tag
    answer = [m for m in d["log"] if m["kind"] == "answer"][0]
    assert answer["reply_snippet"] == "spans on AST nodes?"
    assert d["transcript"] is None and d["message_count"] == 4

    full = get(f"{base}/api/boards/{board.id}?full=1")
    assert [m["id"] for m in full["transcript"]] == sorted(m["id"] for m in full["transcript"])
    assert len(full["transcript"]) == 4


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
