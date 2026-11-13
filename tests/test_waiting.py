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
