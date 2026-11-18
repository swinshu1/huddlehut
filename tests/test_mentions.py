"""Waking an agent when another agent mentions it."""

import threading

import pytest

from huddlehut import hook
from huddlehut.mentions import mention_names, mentions
from test_coordination import ME, OTHER, env, link, repo, run  # noqa: F401  (fixtures)

CODEX = "codex-3f2a"


def test_mention_names_unique_and_shared():
    assert mention_names(CODEX, {"claude-code-1a2b"}) == {CODEX, "3f2a", "codex"}
    assert mention_names("claude-code-1a2b", {CODEX}) == {"claude-code-1a2b", "1a2b", "claude-code", "claude"}
    # two codex sessions: "codex" is ambiguous, so only the full name and tag address this one
    assert mention_names(CODEX, {"codex-9c9c"}) == {CODEX, "3f2a"}


@pytest.mark.parametrize("text,hit", [
    ("@3f2a can you check the seeds?", True),
    ("3f2a: rebuilt, go ahead", True),
    ("codex, review pls", True),
    ("Codex: review pls", True),
    ("codex-3f2a owns that", True),
    ("@all heads up: main is red", True),
    ("all tests pass", False),
    ("committed 3f2a1bc (main): fix", False),
    ("codexify the thing", False),
    ("waiting on e9c0", False),
])
def test_mentions(text, hit):
    assert mentions(text, mention_names(CODEX, {"claude-code-1a2b"})) is hit


def test_idle_agent_woken_by_mention(env, repo, monkeypatch, capsys):
    store, board, key = env
    monkeypatch.setattr(hook, "WAIT_GRACE", 0)
    monkeypatch.setattr(hook, "WAIT_POLL", 0.1)
    monkeypatch.setattr(hook, "WAIT_SECONDS", 10)
    link(board, monkeypatch, capsys, repo)                       # this session is ME (claude-code-aaaa)
    from huddlehut.store import Store
    threading.Timer(0.4, lambda: Store(store.url).post(key, OTHER, "aaaa: snapshot rebuilt, go ahead")).start()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"session_id": "S1"}'))
    code = hook.main(["wait", "--client", "claude"])
    err = capsys.readouterr().err
    assert code == 2 and "new on the board for you" in err and "snapshot rebuilt" in err
    assert "user doesn't see the board" in err


def test_plain_chatter_does_not_wake_or_hold(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    store.post(key, OTHER, "fyi: lunch", "note")
    assert run("stop", {"session_id": "S1"}, monkeypatch, capsys) is None
    store.post(key, OTHER, "@claude can you rerun the milestone check?", "note")
    out = run("stop", {"session_id": "S1"}, monkeypatch, capsys)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "rerun the milestone check" in ctx and ctx.endswith("tell them only if they need to act or decide.")


def test_a_question_wakes_only_the_agents_it_names(store):
    """A question or blocker for someone else isn't yours to answer: it doesn't wake you. One that names
    no agent is for anyone, and mentions, @all and replies to your posts always reach you."""
    from huddlehut.hook import _needs_me

    board, key = store.create_board("b", "p", "claude-code-aaaa")
    for agent, nick in (("claude-code-aaaa", "mochi"), ("codex-bbbb", "otter"), ("claude-code-cccc", "pebble")):
        store.set_nickname(board.id, agent, nick)
    mine = store.post(key, "claude-code-aaaa", "lexer done", "result")
    posts = [
        store.post(key, "codex-bbbb", "@pebble which accounts did you create?", "question"),
        store.post(key, "codex-bbbb", "anyone know the payout rules?", "question"),
        store.post(key, "codex-bbbb", "mochi: need the login code", "blocker"),
        store.post(key, "codex-bbbb", "@all ship today?", "question"),
        store.post(key, "codex-bbbb", "nice", "note", reply_to=mine.id),
        store.post(key, "codex-bbbb", "pilot negative, moving on", "result"),
    ]
    woken = lambda agent: [posts.index(m) for m in _needs_me(store, board.id, agent, store.unread(board.id, agent))]  # noqa: E731
    assert woken("claude-code-aaaa") == [1, 2, 3, 4]
    assert woken("claude-code-cccc") == [0, 1, 3]
