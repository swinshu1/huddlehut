"""An idle Codex agent gets woken: when its Stop hook lets it stop, a background waiter watches the board
and resumes the agent's thread with `codex exec resume` once something needs it."""

import json
import sys
import threading
import time

from huddlehut import codex_wake, hook
from test_hooks import bash, env, link, run  # noqa: F401  (env is a fixture)


def test_codex_stop_starts_a_waiter_only_when_it_lets_the_agent_stop(env, monkeypatch, capsys):
    store, board, key = env
    started = []
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "1")
    monkeypatch.setattr(codex_wake.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    link(board, monkeypatch, capsys, sid="C1", me="codex-cccc")
    stop = {"session_id": "C1", "cwd": "/work"}

    assert run("stop", stop, monkeypatch, capsys, client="codex")[1] is None
    cmd, kw = started[-1]
    assert cmd[1:] == ["-m", "huddlehut.codex_wake", "C1", "/work"] and kw["start_new_session"]

    store.post(key, "claude-code-aaaa", "who owns ast.py?", "question")
    assert run("stop", stop, monkeypatch, capsys, client="codex")[1]["decision"] == "block"   # kept going instead
    run("stop", {**stop, "last_assistant_message": "Ship it?"}, monkeypatch, capsys, client="codex")  # waits on user
    run("stop", stop, monkeypatch, capsys, client="claude")             # Claude Code has its own wake hook
    run("stop", {"session_id": "nobody"}, monkeypatch, capsys, client="codex")   # not on a board
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "0")
    run("stop", stop, monkeypatch, capsys, client="codex")
    assert len(started) == 1


def test_waiter_resumes_the_thread_when_the_board_needs_it(env, db_url, monkeypatch, capsys, tmp_path):
    store, board, key = env
    monkeypatch.setattr(hook, "WAIT_SECONDS", 10)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-config"))      # server name falls back to "huddlehut"
    calls = tmp_path / "calls.json"
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"state = json.load(open({str(hook._state_path('C1'))!r}))\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'waking': 'waking_until' in state}},"
                    f" open({str(calls)!r}, 'w'))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HUDDLEHUT_CODEX_BIN", str(fake))
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "1")

    # the agent joined with its key; the hook keeps it for the wake prompt
    joined = [{"type": "text", "text": f"[{board.id} you=codex-cccc]\nparser [repo]"}]
    run("post-tool", {"session_id": "C1", "tool_name": "mcp__huddlehut__join_board",
                      "tool_input": {"key": key, "nickname": "acorn"}, "tool_response": joined},
        monkeypatch, capsys, client="codex")
    store.set_nickname(board.id, "codex-cccc", "acorn")

    result = {}
    waiter = threading.Thread(target=lambda: result.update(code=codex_wake.run("C1", str(tmp_path))))
    waiter.start()
    time.sleep(0.3)
    store.post(key, "claude-code-aaaa", "fyi lexer merged", "note")      # doesn't need it: keeps waiting
    time.sleep(0.3)
    assert not calls.exists()
    store.post(key, "claude-code-aaaa", "@acorn are the seeds rebuilt?", "note")
    waiter.join(10)

    assert result["code"] == 0
    call = json.loads(calls.read_text())
    argv = call["argv"]
    assert argv[:5] == ["exec", "--skip-git-repo-check", "-C", str(tmp_path), "resume"]
    assert argv[5:7] == ["-c", 'mcp_servers.huddlehut.env.HUDDLEHUT_SESSION="C1"']
    assert f"mcp_servers.huddlehut.env.HUDDLEHUT_DATABASE_URL={json.dumps(db_url)}" in argv  # same database
    assert argv[-2] == "C1"
    prompt = argv[-1]
    assert "@acorn are the seeds rebuilt?" in prompt
    assert f"join_board(key='{key}', nickname='acorn')" in prompt
    assert call["waking"]                                                 # its SessionEnd won't mark acorn left
    assert store.identity("codex:C1", "codex-zzzz") == "codex-cccc"      # its board server comes back as acorn
    assert store.unread(board.id, "codex-cccc") == []                     # delivered once
    assert "waking_until" not in hook.load("C1")
    assert "waking codex-cccc for" in (hook.HOME / "wake.log").read_text()


def test_waiter_steps_aside_when_codex_works_again(env, monkeypatch, capsys):
    store, board, key = env
    monkeypatch.setattr(hook, "WAIT_SECONDS", 10)
    link(board, monkeypatch, capsys, sid="C1", me="codex-cccc")
    result = {}
    waiter = threading.Thread(target=lambda: result.update(code=codex_wake.run("C1", "/")))
    waiter.start()
    time.sleep(0.3)
    run("post-tool", bash("C1"), monkeypatch, capsys, client="codex")    # the user gave it a new turn
    store.post(key, "claude-code-aaaa", "@codex-cccc ping", "note")
    waiter.join(5)
    assert not waiter.is_alive() and result["code"] is None


def test_session_end_of_a_woken_turn_keeps_the_agent_on_the_board(env, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, sid="C1", me="codex-cccc")
    store.set_nickname(board.id, "codex-cccc", "acorn")
    state = hook.load("C1")
    hook.save("C1", {**state, "waking_until": time.time() + 60})
    link(board, monkeypatch, capsys, sid="C1", me="codex-cccc")            # the woken turn rejoins first
    run("session-end", {"session_id": "C1"}, monkeypatch, capsys, client="codex")
    assert hook.load("C1") and store.presence(board.id)["codex-cccc"]["state"] == "active"

    hook.save("C1", {**state, "waking_until": time.time() - 1})          # a stale flag doesn't pin it forever
    run("session-end", {"session_id": "C1"}, monkeypatch, capsys, client="codex")
    assert not hook.load("C1") and store.presence(board.id)["codex-cccc"]["state"] == "left"


def test_key_from_create_board_and_server_name_from_codex_config(env, monkeypatch, capsys, tmp_path):
    store, board, key = env
    made = [{"type": "text", "text": f"[{board.id} you=codex-cccc]\nkey {key} (share it)"}]
    run("post-tool", {"session_id": "C2", "tool_name": "mcp__huddlehut__create_board", "tool_response": made},
        monkeypatch, capsys, client="codex")
    assert hook.load("C2")["key"] == key

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_wake.server_name() == "huddlehut"
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.other]\ncommand = "x"\n'
        '[mcp_servers.board]\ncommand = "uv"\nargs = ["run", "--directory", "/src/huddlehut", "huddlehut-mcp"]\n')
    assert codex_wake.server_name() == "board"
