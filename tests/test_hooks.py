import io
import json
import threading

import pytest

from huddlehut import hook


@pytest.fixture
def env(db_url, store, tmp_path, monkeypatch):
    monkeypatch.setenv("HUDDLEHUT_DATABASE_URL", db_url)
    monkeypatch.setattr(hook, "HOME", tmp_path / "home")
    monkeypatch.setattr(hook, "SESSIONS", tmp_path / "home" / "sessions")
    monkeypatch.setattr(hook, "INTERVAL", 0)
    monkeypatch.setattr(hook, "WAIT_GRACE", 0)
    monkeypatch.setattr(hook, "WAIT_POLL", 0.1)
    board, key = store.create_board("parser", "repo", "claude-code-aaaa")
    return store, board, key


def run(event, payload, monkeypatch, capsys, client="claude"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = hook.main([event, "--client", client])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def link(board, monkeypatch, capsys, sid="S1", me="claude-code-aaaa"):
    resp = [{"type": "text", "text": f"[{board.id} you={me}]\nparser [repo]\nlast_id=0"}]
    return run("post-tool", {"session_id": sid, "tool_name": "mcp__plugin_huddlehut_board__join_board",
                             "tool_response": resp}, monkeypatch, capsys)


def bash(sid="S1"):
    return {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}}


def test_unlinked_session_gets_nothing(env, monkeypatch, capsys):
    store, board, key = env
    store.post(key, "codex-bbbb", "hello")
    assert run("post-tool", bash("other"), monkeypatch, capsys)[1] is None


def test_linked_session_gets_new_messages_once(env, monkeypatch, capsys):
    store, board, key = env
    assert link(board, monkeypatch, capsys)[1] is None   # board tool result already had the messages
    store.post(key, "claude-code-aaaa", "my own post")
    store.post(key, "codex-bbbb", "tests green", "result")
    _, out, _ = run("post-tool", bash(), monkeypatch, capsys)
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    assert "bbbb/result: tests green" in ctx["additionalContext"]
    assert "my own post" not in ctx["additionalContext"]
    assert run("post-tool", bash(), monkeypatch, capsys)[1] is None          # not delivered twice
    assert store.unread(board.id, "claude-code-aaaa") == []                 # the MCP server agrees


def test_throttled_between_checks(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys)
    run("post-tool", bash(), monkeypatch, capsys)        # first call after joining always checks
    monkeypatch.setattr(hook, "INTERVAL", 100)
    store.post(key, "codex-bbbb", "hi")
    assert run("post-tool", bash(), monkeypatch, capsys)[1] is None


def test_closed_board_unlinks(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys)
    store.close_board(key, "codex-bbbb")
    _, out, _ = run("post-tool", bash(), monkeypatch, capsys)
    assert "was closed" in out["hookSpecificOutput"]["additionalContext"]
    assert hook.load("S1") == {}


def test_stop_continues_only_for_things_that_need_me(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys)
    mine = store.post(key, "claude-code-aaaa", "Pratt parser?", "question")
    store.post(key, "codex-bbbb", "fyi lunch", "note")
    assert run("stop", {"session_id": "S1"}, monkeypatch, capsys)[1] is None  # chatter doesn't keep it going

    store.post(key, "codex-bbbb", "yes", "answer", reply_to=mine.id)
    _, out, _ = run("stop", {"session_id": "S1", "stop_hook_active": True}, monkeypatch, capsys)
    assert out is None                                                       # loop guard
    _, out, _ = run("stop", {"session_id": "S1"}, monkeypatch, capsys)
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert f"re #{mine.id}" in out["hookSpecificOutput"]["additionalContext"]

    store.post(key, "codex-bbbb", "who owns ast.py?", "question")
    _, out, _ = run("stop", {"session_id": "S1"}, monkeypatch, capsys, client="codex")
    assert out["decision"] == "block" and "who owns ast.py?" in out["reason"]


def test_session_start_reinjects_view_after_compaction(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys)
    store.post(key, "codex-bbbb", "tests in tests/parser", "status")
    assert run("session-start", {"session_id": "S1", "source": "startup"}, monkeypatch, capsys)[1] is None
    _, out, _ = run("session-start", {"session_id": "S1", "source": "compact"}, monkeypatch, capsys)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "you are claude-code-aaaa" in text and "Now:" in text and "tests in tests/parser" in text


def test_wait_wakes_on_reply(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys)
    monkeypatch.setattr(hook, "WAIT_SECONDS", 10)
    q = store.post(key, "claude-code-aaaa", "need review of #3", "question")
    from huddlehut.store import Store
    threading.Timer(0.5, lambda: Store(store.url).post(key, "codex-bbbb", "looks good", "answer", reply_to=q.id)).start()
    code, _, err = run("wait", {"session_id": "S1"}, monkeypatch, capsys)
    assert code == 2 and "looks good" in err


def test_wait_steps_aside_when_agent_resumes(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys)
    monkeypatch.setattr(hook, "WAIT_SECONDS", 10)

    def resume():
        st = hook.load("S1")
        st["active"] = st.get("active", 0) + 1e9
        hook.save("S1", st)

    threading.Timer(0.3, resume).start()
    code, _, _ = run("wait", {"session_id": "S1"}, monkeypatch, capsys)
    assert code == 0


def test_hook_errors_never_escape(env, monkeypatch, capsys):
    store, board, key = env
    hook.save("S1", {"board": "b_deadbeef", "me": "x", "checked": 0})
    code, out, _ = run("post-tool", bash(), monkeypatch, capsys)
    assert code == 0 and out is None
    assert "no board b_deadbeef" in (hook.HOME / "hook.log").read_text()
