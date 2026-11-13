"""An agent waiting on the user isn't re-prompted or woken by the board until the user answers."""

import io
import time

from huddlehut import hook
from test_coordination import ME, OTHER, env, link, repo, run  # noqa: F401  (fixtures)

WAITING = "Still waiting on you. End the `/goal`, or say “rerun” when you want the measurement."


def test_stop_lets_an_agent_waiting_on_the_user_stop(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    store.post(key, OTHER, "@aaaa can you rerun the milestone check?", "question")
    assert run("stop", {"session_id": "S1", "last_assistant_message": WAITING}, monkeypatch, capsys) is None
    assert len(store.unread(board.id, ME)) == 1                 # kept for its next turn, not consumed
    out = run("stop", {"session_id": "S1", "last_assistant_message": "Done. tests pass."}, monkeypatch, capsys)
    assert "rerun the milestone check" in out["hookSpecificOutput"]["additionalContext"]


def test_waiter_does_not_wake_an_agent_waiting_on_the_user(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    monkeypatch.setattr(hook, "WAIT_GRACE", 0)
    store.post(key, OTHER, "@aaaa ping", "note")
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"session_id": "S1", "last_assistant_message": "Should I commit these?"}'))
    started = time.time()
    assert hook.main(["wait", "--client", "claude"]) == 0
    assert time.time() - started < 1                             # returned at once, no waiting loop
    assert len(store.unread(board.id, ME)) == 1


def test_waiting_on_user_is_recorded_then_cleared(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    run("stop", {"session_id": "S1", "last_assistant_message": WAITING}, monkeypatch, capsys)
    assert store.presence(board.id)[ME]["waiting"] == WAITING
    run("post-tool", {"session_id": "S1", "cwd": str(repo), "tool_name": "Bash",          # user answered,
                      "tool_input": {"command": "ls"}, "tool_response": {}}, monkeypatch, capsys)  # it's working
    assert store.presence(board.id)[ME]["waiting"] is None
    run("stop", {"session_id": "S1", "last_assistant_message": "Should I commit?"}, monkeypatch, capsys)
    run("stop", {"session_id": "S1", "last_assistant_message": "Committed. tests pass."}, monkeypatch, capsys)
    assert store.presence(board.id)[ME]["waiting"] is None


def test_pin_keeps_the_ask_not_a_mid_word_tail():
    from huddlehut.store import ask_tail
    reply = "Ran the hard tier. " * 40 + "\n\nStill waiting on you. Say rerun when you want the measurement."
    assert ask_tail(reply) == "Still waiting on you. Say rerun when you want the measurement."
    long_para = "Context sentence number one goes here. " * 30 + "Should I launch hard12v2?"
    t = ask_tail(long_para)
    assert len(t) <= 500 and t.endswith("Should I launch hard12v2?") and t[0].isupper()


def test_pin_keeps_only_the_asking_sentences():
    from huddlehut.store import ask_tail
    reply = ("Results are in: 3/12 attacker wins. The environment has credentials that work.\n\n"
             "I posted the fix on the board for the other session. Should I launch hard12v2 now? "
             "It takes about 40 minutes.")
    assert ask_tail(reply) == "Should I launch hard12v2 now?"


def test_reports_that_mention_the_user_are_not_asks():
    from huddlehut.hook import awaiting_user
    assert not awaiting_user("The other session has already started the rerun with your go-ahead, so there's nothing left to tell it.")
    assert awaiting_user("Ready to merge. I need your go-ahead.")
    assert awaiting_user("Two options above. Your call.")
