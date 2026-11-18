"""Subagents on a board. Codex reports a subagent's hook events under its parent's session_id plus the
subagent's own agent_id, and runs it with its own board server, so it is its own agent with its own link.
It fires no Stop or SessionEnd hooks; its parent's hooks end its board session when its task finishes."""

import json

from huddlehut import hook
from test_hooks import env, run  # noqa: F401  (env is a fixture)

OTHER = "claude-code-aaaa"


def transcript(path, *events):
    path.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": e}}) + "\n" for e in events))
    return str(path)


def codex_join(board, me, monkeypatch, capsys, sid="P1", agent_id=None, transcript_path=None):
    inp = {"session_id": sid, "tool_name": "mcp__huddlehut__join_board", "tool_input": {"key": "x"},
           "tool_response": [{"type": "text", "text": f"[{board.id} you={me}]\nb [repo]"}]}
    if agent_id:
        inp |= {"agent_id": agent_id, "agent_type": "default"}
    if transcript_path:
        inp["transcript_path"] = transcript_path
    return run("post-tool", inp, monkeypatch, capsys, client="codex")


def shell(sid, agent_id=None):
    inp = {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {}}
    return inp | ({"agent_id": agent_id, "agent_type": "default"} if agent_id else {})


def test_transcript_says_whether_a_thread_is_mid_task(tmp_path):
    # a forked subagent starts with its parent's history, then runs its own task
    forked = ["task_started", "turn_aborted", "task_started"]
    assert hook.thread_running(transcript(tmp_path / "a", *forked)) is True
    assert hook.thread_running(transcript(tmp_path / "b", *forked, "task_complete")) is False
    assert hook.thread_running(transcript(tmp_path / "c", "token_count")) is None
    assert hook.thread_running(str(tmp_path / "missing")) is None and hook.thread_running(None) is None


def test_subagent_gets_its_own_link_and_identity(env, monkeypatch, capsys, tmp_path):
    store, board, key = env
    codex_join(board, "codex-0a0a", monkeypatch, capsys)                          # the parent, otter
    t = transcript(tmp_path / "sub.jsonl", "task_started")
    codex_join(board, "codex-5b5b", monkeypatch, capsys, agent_id="A1", transcript_path=t)  # its subagent

    assert hook.load("P1")["me"] == "codex-0a0a"                                  # parent's link untouched
    assert hook.load("A1") | {"active": 0} == hook.load("A1") | {"active": 0, "board": board.id,
                                                                 "me": "codex-5b5b", "parent": "P1", "transcript": t}
    store.post(key, OTHER, "@codex-5b5b which seeds?", "question")
    _, out, _ = run("post-tool", shell("P1", agent_id="A1"), monkeypatch, capsys, client="codex")
    assert "which seeds?" in out["hookSpecificOutput"]["additionalContext"]       # delivered to the subagent
    assert store.unread(board.id, "codex-5b5b") == []
    assert len(store.unread(board.id, "codex-0a0a")) == 1                        # the parent still has it


def test_parent_ends_its_subagents_when_their_tasks_finish(env, monkeypatch, capsys, tmp_path):
    store, board, key = env
    running = tmp_path / "running.jsonl"
    subs = {"codex-1111": ("A1", transcript(running, "task_started")),
            "codex-2222": ("A2", transcript(tmp_path / "done.jsonl", "task_started", "task_complete")),
            "codex-3333": ("A3", None)}                                          # no transcript: can't tell
    for me, (agent_id, t) in subs.items():
        codex_join(board, me, monkeypatch, capsys, agent_id=agent_id, transcript_path=t)  # parent never joined
        store.touch(board.id, me)
    promise = store.post(key, "codex-2222", "post when seeds rebuilt", "promise")
    state = lambda me: store.presence(board.id)[me]["state"]  # noqa: E731

    run("stop", {"session_id": "P1"}, monkeypatch, capsys, client="codex")        # the parent's turn ends
    assert [state(m) for m in subs] == ["active", "left", "active"]
    assert not hook.load("A2") and hook.load("A1") and hook.load("A3")
    blocker = store.all_messages(board.id)[-1]                                   # its unkept promise is flagged
    assert blocker.kind == "blocker" and blocker.author == "codex-2222" and f"#{promise.id}" in blocker.content

    transcript(running, "task_started", "task_complete")
    wait = {"session_id": "P1", "tool_name": "collaborationwait_agent", "tool_input": {}, "tool_response": "{}"}
    run("post-tool", wait, monkeypatch, capsys, client="codex")                   # it waited on them
    assert [state(m) for m in subs] == ["left", "left", "active"]

    run("session-end", {"session_id": "P1"}, monkeypatch, capsys, client="codex")  # none outlive it
    assert state("codex-3333") == "left" and hook.subagents_of("P1") == []
    assert store.all_messages(board.id)[-1].id == blocker.id                      # leaving quietly otherwise


def test_parent_session_end_ends_the_parent_too(env, monkeypatch, capsys, tmp_path):
    store, board, key = env
    codex_join(board, "codex-0a0a", monkeypatch, capsys)
    codex_join(board, "codex-5b5b", monkeypatch, capsys, agent_id="A1")
    for me in ("codex-0a0a", "codex-5b5b"):
        store.touch(board.id, me)
    run("session-end", {"session_id": "P1"}, monkeypatch, capsys, client="codex")
    assert {a: p["state"] for a, p in store.presence(board.id).items() if a.startswith("codex")} == \
        {"codex-0a0a": "left", "codex-5b5b": "left"}
    assert not hook.load("P1") and not hook.load("A1")


def test_claude_code_subagents_share_the_parent_link(env, monkeypatch, capsys):
    """A Claude Code subagent runs on its parent's board server, so its events count as the parent's."""
    store, board, key = env
    run("post-tool", {"session_id": "S1", "tool_name": "mcp__plugin_huddlehut_board__join_board",
                      "tool_response": [{"type": "text", "text": f"[{board.id} you={OTHER}]"}]}, monkeypatch, capsys)
    store.post(key, "codex-bbbb", "tests green", "result")
    _, out, _ = run("post-tool", shell("S1", agent_id="sub-1"), monkeypatch, capsys)
    assert "tests green" in out["hookSpecificOutput"]["additionalContext"] and hook.subagents_of("S1") == []


def test_departure_is_quiet_unless_a_promise_is_left(store):
    board, key = store.create_board("b", "p", OTHER)
    store.touch(board.id, "codex-1111")
    assert store.end_session(board.id, "codex-1111", by="codex-1111", reason="ended", announce=False) is None
    store.touch(board.id, "codex-2222")
    store.post(key, "codex-2222", "post when seeds rebuilt", "promise")
    m = store.end_session(board.id, "codex-2222", by="codex-2222", reason="ended", announce=False)
    assert m.kind == "blocker" and "post when seeds rebuilt" in m.content and "Unkept promise" in m.content


def test_a_long_busy_turn_still_reads_as_busy(tmp_path):
    """A turn can write megabytes after its task_started; the reader looks back past them."""
    path = tmp_path / "long.jsonl"
    filler = json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 5000}})
    with open(path, "w") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        for _ in range(400):                                                   # ~2 MB of tool output
            f.write(filler + "\n")
    assert hook.thread_running(str(path)) is True
    assert hook.thread_running(str(path), limit=1 << 16) is None              # past the limit: unknown
