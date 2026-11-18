"""Idle Codex agents get woken. Codex's Stop hook marks a thread idle; one wake service per machine
watches every linked Codex thread and resumes an idle one with `codex exec resume` when the board
needs its agent."""

import json
import re
import sys
import threading
import time
from concurrent.futures import Executor, Future

from huddlehut import codex_wake, hook, wake_service
from test_hooks import env, link, run  # noqa: F401  (env is a fixture)


class Inline(Executor):
    """Runs submitted work right away, so a test sees a tick's wakes finish."""

    def submit(self, fn, *args, **kwargs):
        f = Future()
        f.set_result(fn(*args, **kwargs))
        return f


def transcript(path, *events):
    path.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": e}}) + "\n" for e in events))
    return str(path)


def codex_link(board, me, monkeypatch, capsys, sid="C1", key="", transcript_path=None):
    inp = {"session_id": sid, "tool_name": "mcp__huddlehut__join_board", "tool_input": {"key": key},
           "tool_response": [{"type": "text", "text": f"[{board.id} you={me}]\nb [repo]"}]}
    if transcript_path:
        inp["transcript_path"] = transcript_path
    return run("post-tool", inp, monkeypatch, capsys, client="codex")


def test_codex_stop_marks_the_thread_idle_and_keeps_the_service_up(env, monkeypatch, capsys, tmp_path):
    store, board, key = env
    ensured = []
    monkeypatch.setattr(wake_service, "ensure", lambda: ensured.append(1) or True)
    t = transcript(tmp_path / "t.jsonl", "task_started", "task_complete")
    codex_link(board, "codex-cccc", monkeypatch, capsys, transcript_path=t)
    assert hook.load("C1")["client"] == "codex" and hook.load("C1")["transcript"] == t   # recorded on use

    run("stop", {"session_id": "C1", "cwd": "/work"}, monkeypatch, capsys, client="codex")
    state = hook.load("C1")
    assert state["stopped_at"] >= state["active"] and state["awaiting_user"] is False and state["cwd"] == "/work"
    assert ensured == [1]

    store.post(key, "claude-code-aaaa", "who owns ast.py?", "question")
    assert run("stop", {"session_id": "C1"}, monkeypatch, capsys, client="codex")[1]["decision"] == "block"
    assert ensured == [1]                                                    # kept going: not idle
    run("stop", {"session_id": "C1", "last_assistant_message": "Ship it?"}, monkeypatch, capsys, client="codex")
    assert hook.load("C1")["awaiting_user"] is True                          # the service leaves it be
    link(board, monkeypatch, capsys, sid="S1")
    run("stop", {"session_id": "S1"}, monkeypatch, capsys, client="claude")  # Claude Code wakes itself
    assert len(ensured) == 2


def test_one_service_per_machine(env, monkeypatch):
    started = []
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "1")
    monkeypatch.setattr(wake_service.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    held = wake_service._lock()                                              # a service is running
    assert wake_service.running() and not wake_service.ensure() and started == []
    held.close()
    assert not wake_service.running() and wake_service.ensure()
    cmd, kw = started[0]
    assert cmd[1:] == ["-m", "huddlehut.wake_service"] and kw["start_new_session"]
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "0")
    assert not wake_service.ensure() and len(started) == 1


def test_due_picks_idle_codex_threads_whose_agent_is_needed(env, tmp_path):
    store, board, key = env
    done = transcript(tmp_path / "done", "task_started", "task_complete")
    busy = transcript(tmp_path / "busy", "task_complete", "task_started")
    now = time.time()
    links = {
        "idle": {"client": "codex", "me": "codex-0001", "transcript": done},
        "mid-turn": {"client": "codex", "me": "codex-0002", "transcript": busy},
        "asks-user": {"client": "codex", "me": "codex-0003", "transcript": done, "awaiting_user": True},
        "subagent": {"client": "codex", "me": "codex-0004", "transcript": done, "parent": "idle"},
        "claude": {"client": "claude", "me": "claude-code-0005"},
        "stopped": {"client": "codex", "me": "codex-0006", "active": now - 9, "stopped_at": now - 5},
        "unknown": {"client": "codex", "me": "codex-0007", "active": now - 9},   # never seen stopping
        "left": {"client": "codex", "me": "codex-0008", "transcript": done},
        "older": {"me": "codex-0009", "transcript": done},                       # linked before client was kept
    }
    for k, state in links.items():
        hook.save(k, {"board": board.id, **state})
        store.touch(board.id, state["me"])
    store.mark_left(board.id, "codex-0008")
    assert {k for k, _ in wake_service.codex_threads()} == set(links) - {"subagent", "claude"}
    assert wake_service.due(store, wake_service.codex_threads()) == []      # nothing needs anyone yet

    store.post(key, "claude-code-aaaa", "@all rebase on main", "note")
    assert sorted(k for k, _, _ in wake_service.due(store, wake_service.codex_threads())) == ["idle", "older", "stopped"]
    assert [k for k, _, _ in wake_service.due(store, wake_service.codex_threads(), busy={"idle", "older"})] == ["stopped"]


def test_a_tick_queues_what_needs_the_agent_to_its_thread(env, monkeypatch, capsys, tmp_path):
    store, board, key = env
    calls = tmp_path / "calls.json"
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"state = json.load(open({str(hook._state_path('C1'))!r}))\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'waking': 'waking_until' in state}},"
                    f" open({str(calls)!r}, 'w'))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HUDDLEHUT_CODEX_BIN", str(fake))
    monkeypatch.setenv("HUDDLEHUT_CODEX_WAKE", "1")
    monkeypatch.setattr(wake_service, "ensure", lambda: False)
    t = transcript(tmp_path / "t.jsonl", "task_started", "task_complete")
    codex_link(board, "codex-cccc", monkeypatch, capsys, key=key, transcript_path=t)
    store.set_nickname(board.id, "codex-cccc", "acorn")
    run("stop", {"session_id": "C1", "cwd": str(tmp_path)}, monkeypatch, capsys, client="codex")

    busy = set()
    store.post(key, "claude-code-aaaa", "fyi lexer merged", "note")         # doesn't need it
    assert wake_service.tick(store, busy, Inline()) == [] and not calls.exists()
    store.post(key, "claude-code-aaaa", "@acorn are the seeds rebuilt?", "note")
    assert wake_service.tick(store, busy, Inline()) == ["C1"] and busy == set()

    call = json.loads(calls.read_text())
    argv = call["argv"]
    # queued to the thread: whatever has it open (the Codex app) takes it as a turn; else the daemon resumes it
    assert argv[:4] == ["queue", "--thread", "C1", "--message"] and argv[5:] == ["--resume"] and call["waking"]
    prompt = argv[4]
    assert "@acorn are the seeds rebuilt?" in prompt
    ticket = re.search(rf"join_board\(key='{key}', nickname='acorn', ticket='(wk_[\w-]+)'\)", prompt).group(1)
    assert store.redeem_ticket(ticket, board.id) == "codex-cccc"            # a resumed board server becomes acorn
    assert store.unread(board.id, "codex-cccc") == []                      # delivered once
    assert hook.load("C1")["waking_until"] > time.time()                   # and not woken again meanwhile
    log = (hook.HOME / "wake.log").read_text()
    assert "waking codex-cccc for" in log and "queued for codex-cccc, codex exit 0" in log
    store.post(key, "claude-code-aaaa", "@acorn also the lexer?", "note")
    assert wake_service.tick(store, busy, Inline()) == []


def test_service_runs_alone_and_leaves_when_nothing_is_linked(env, monkeypatch):
    monkeypatch.setattr(wake_service, "LINGER", 0.2)
    monkeypatch.setattr(wake_service, "POLL", 0.05)
    held = wake_service._lock()
    assert wake_service.serve() == 0                                         # another one runs: step aside
    held.close()
    done = threading.Event()
    threading.Thread(target=lambda: (wake_service.serve(), done.set()), daemon=True).start()
    assert done.wait(5)
    assert "stopped: no Codex threads to watch" in (hook.HOME / "wake.log").read_text()


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


def test_the_link_keeps_the_key_for_the_wake_message(env, monkeypatch, capsys):
    store, board, key = env
    made = [{"type": "text", "text": f"[{board.id} you=codex-cccc]\nkey {key} (share it)"}]
    run("post-tool", {"session_id": "C2", "tool_name": "mcp__huddlehut__create_board", "tool_response": made},
        monkeypatch, capsys, client="codex")
    assert hook.load("C2")["key"] == key


def test_wake_tickets_work_once_on_their_board_while_fresh(env, monkeypatch):
    store, board, key = env
    other, _ = store.create_board("other", "repo", "claude-code-aaaa")
    t = store.issue_ticket(board.id, "codex-cccc")
    assert store.redeem_ticket(t, other.id) is None                        # another board: no, and it's spent
    t = store.issue_ticket(board.id, "codex-cccc")
    assert store.redeem_ticket(t, board.id) == "codex-cccc" and store.redeem_ticket(t, board.id) is None
    t = store.issue_ticket(board.id, "codex-cccc")
    monkeypatch.setattr("huddlehut.store.TICKET_TTL", -1)
    assert store.redeem_ticket(t, board.id) is None                         # stale
