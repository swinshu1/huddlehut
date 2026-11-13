"""Nicknames and model tags."""

import pytest

from huddlehut.compact import render_view
from huddlehut.store import default_nickname, short_model
from test_coordination import ME, OTHER, env, link, repo, run  # noqa: F401  (fixtures)


def test_nickname_rules(store):
    board, _ = store.create_board("b", "p", ME)
    assert store.set_nickname(board.id, ME, "Mochi") == ("mochi", None)
    nick, why = store.set_nickname(board.id, OTHER, "mochi")
    assert nick != "mochi" and why == "'mochi' is taken"
    for bad in ("all", "x", "1abc", "claude"):
        assert store.set_nickname(board.id, "codex-cccc", bad)[1]          # refused, with a reason
    assert store.set_nickname(board.id, ME)[0] == "mochi"                   # keeps its name when not asked
    assert store.set_nickname(board.id, ME, "pebble") == ("pebble", None)  # can rename itself
    assert len(set(store.nicknames(board.id).values())) == 3               # always unique


def test_default_nickname_is_stable_and_skips_taken():
    a = default_nickname("codex-3f2a")
    assert a == default_nickname("codex-3f2a")
    assert default_nickname("codex-3f2a", taken={a}) != a


@pytest.mark.parametrize("model,label", [
    ("claude-opus-5-5", "opus 5.5"), ("claude-sonnet-5-5", "sonnet 5.5"), ("claude-haiku-4-5-20251001", "haiku 4.5"),
    ("claude-fable-5-2", "fable 5.2"), ("gpt-5-codex", "gpt-5-codex"), ("", ""),
])
def test_short_model(model, label):
    assert short_model(model) == label


def test_view_uses_nicknames_and_shows_models_in_roster(store):
    board, key = store.create_board("b", "p", ME)
    store.set_nickname(board.id, ME, "mochi")
    store.set_model(board.id, ME, "claude-opus-5-5")
    store.post(key, ME, "lexer", "status")
    store.post(key, OTHER, "spans?", "question")
    text, _ = render_view(store, board)
    assert "#1 mochi (opus 5.5): lexer" in text
    assert "#2 bbbb/question: spans?" in text


def test_mention_by_nickname_holds_stop(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    store.set_nickname(board.id, ME, "pebble")
    store.post(key, OTHER, "@pebble snapshot rebuilt", "note")
    out = run("stop", {"session_id": "S1"}, monkeypatch, capsys)
    assert "snapshot rebuilt" in out["hookSpecificOutput"]["additionalContext"]


def test_codex_hook_reports_model(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    run("post-tool", {"session_id": "S1", "cwd": str(repo), "tool_name": "Bash", "model": "gpt-5-codex",
                      "tool_input": {"command": "ls"}, "tool_response": {}}, monkeypatch, capsys)
    assert store.presence(board.id)[ME]["model"] == "gpt-5-codex"


def test_unnamed_session_gets_a_nickname_from_its_hooks(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    out = run("post-tool", {"session_id": "S1", "cwd": str(repo), "tool_name": "Bash",
                            "tool_input": {"command": "ls"}, "tool_response": {}}, monkeypatch, capsys)
    nick = store.nicknames(board.id)[ME]
    assert f"you're {nick} on this board now" in out["hookSpecificOutput"]["additionalContext"]
    again = run("post-tool", {"session_id": "S1", "cwd": str(repo), "tool_name": "Bash",
                              "tool_input": {"command": "ls"}, "tool_response": {}}, monkeypatch, capsys)
    assert again is None                                        # told once
