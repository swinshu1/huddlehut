"""File locks, and keeping file names out of posts."""

import time

from huddlehut.compact import render_view
from test_coordination import ME, OTHER, bash, env, git, link, repo, run  # noqa: F401  (fixtures)
from test_inventory import edit


def held(store, board):
    return {(c["agent"], c["path"], c["locked"]) for c in store.claims(board.id)}


# ---- locks ------------------------------------------------------------------


def test_lock_unlock_and_takeover(store):
    board, _ = store.create_board("b", "p", ME)
    store.touch(board.id, ME)
    store.touch(board.id, OTHER)
    store.claim(board.id, OTHER, ["src/api.py"], "/r", lock=True)
    assert held(store, board) == {(OTHER, "src/api.py", 1)}
    store.claim(board.id, OTHER, ["src/api.py"], "/r")              # re-claim keeps the lock
    assert held(store, board) == {(OTHER, "src/api.py", 1)}
    store.unlock(board.id, OTHER, ["src/api.py"])
    assert held(store, board) == {(OTHER, "src/api.py", 0)}         # still claimed
    store.claim(board.id, OTHER, ["src/api.py"], "/r", lock=True)
    store.mark_left(board.id, OTHER)
    store.claim(board.id, ME, ["src/api.py"], "/r")                # holder gone: taken over, lock dropped
    assert held(store, board) == {(ME, "src/api.py", 0)}


def test_view_marks_locked_files(store):
    board, _ = store.create_board("b", "p", ME)
    store.claim(board.id, ME, ["src/a.py"], lock=True)
    store.claim(board.id, ME, ["src/b.py"])
    assert "aaaa: src/a.py (locked), src/b.py" in render_view(store, board)[0]


def _other_holds(store, board, repo, lock):
    top = git(repo, "rev-parse", "--show-toplevel")
    store.touch(board.id, OTHER, str(repo), top, "main")
    store.claim(board.id, OTHER, ["src/api.py"], top, lock=lock)


def test_locked_file_blocks_every_edit_until_unlocked(env, repo, monkeypatch, capsys):
    store, board, key = env
    _other_holds(store, board, repo, lock=True)
    link(board, monkeypatch, capsys, repo)
    for _ in range(2):                                               # no retry bypass
        out = run("pre-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("[huddlehut] Locked: src/api.py held by")
    store.unlock(board.id, OTHER)
    out = run("pre-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)
    assert "Edit again to proceed" in out["hookSpecificOutput"]["permissionDecisionReason"]   # soft now
    assert run("pre-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys) is None


def test_lock_lapses_when_holder_goes_idle(env, repo, monkeypatch, capsys):
    store, board, key = env
    _other_holds(store, board, repo, lock=True)
    link(board, monkeypatch, capsys, repo)
    store.db.execute("UPDATE presence SET seen_at=? WHERE agent=?", (time.time() - 3600, OTHER))
    assert run("pre-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys) is None


def test_locked_file_blocks_commits_every_time(env, repo, monkeypatch, capsys):
    store, board, key = env
    _other_holds(store, board, repo, lock=True)
    link(board, monkeypatch, capsys, repo)
    (repo / "src" / "api.py").write_text("x = 7\n")
    for _ in range(2):
        out = run("pre-tool", bash("git commit -am all", repo), monkeypatch, capsys)
        assert "locked files" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---- file names stay out of posts ---------------------------------------------


def test_status_ownership_goes_to_inventory_not_text(store):
    board, key = store.create_board("b", "p", ME)
    m = store.post(key, ME, "Refactor parser; own src/parser/*, tests/test_parser.py", "status")
    assert m.content == "Refactor parser"
    assert {(c["agent"], c["path"]) for c in store.claims(board.id)} == {(ME, "src/parser/*"), (ME, "tests/test_parser.py")}
    assert store.post(key, ME, "own src/api.py", "status").content == "working"   # nothing but files
    for short in ("42", "ok", "no"):                                              # short posts aren't file names
        assert store.post(key, ME, short, "answer").content == short
        assert store.post(key, ME, short, "note").content == short


def test_status_naming_someone_elses_file_is_reported(store):
    board, key = store.create_board("b", "p", ME)
    store.touch(board.id, OTHER)
    store.claim(board.id, OTHER, ["src/api.py"])
    m = store.post(key, ME, "routes work; own src/api.py", "status")
    assert [(p, c["agent"]) for p, c in m.conflicts] == [("src/api.py", OTHER)]
    assert "src/api.py" in m.content                                  # not mine: the name stays


def test_own_files_stripped_from_notes_but_not_decisions(store):
    board, key = store.create_board("b", "p", ME)
    store.claim(board.id, ME, ["src/lexer.py", "src/models.py"])
    store.claim(board.id, OTHER, ["src/api.py"])
    assert store.post(key, ME, "Refactored lexer (src/lexer.py). tests pass.", "note").content == "Refactored lexer. tests pass."
    assert store.post(key, ME, "need src/api.py from codex", "note").content == "need src/api.py from codex"
    assert store.post(key, ME, "keep schema in src/models.py: one source of truth", "decision").content == \
        "keep schema in src/models.py: one source of truth"


def test_changed_file_lists_dropped_everywhere(store):
    _, key = store.create_board("b", "p", ME)
    m = store.post(key, ME, "Fixed auth bug. Files changed: src/auth.py, tests/test_auth.py. tests pass", "result")
    assert m.content == "Fixed auth bug. tests pass"
