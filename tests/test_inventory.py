"""Shared file inventory: claims, the Files view, auto-claim on edit, release on commit/exit, edit guard."""

import time

from huddlehut.compact import render_view
from test_coordination import ME, OTHER, bash, env, git, link, repo, run  # noqa: F401  (fixtures)


def edit(path, cwd, sid="S1"):
    return {"session_id": sid, "cwd": str(cwd), "tool_name": "Edit",
            "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"}, "tool_response": {}}


def patch(paths, cwd, sid="S1"):
    body = "*** Begin Patch\n" + "".join(f"*** Update File: {p}\n@@\n-a\n+b\n" for p in paths) + "*** End Patch"
    return {"session_id": sid, "cwd": str(cwd), "tool_name": "apply_patch", "tool_input": {"command": body},
            "tool_response": {}}


def holders(store, board):
    return {(c["agent"], c["path"]) for c in store.claims(board.id)}


# ---- store ------------------------------------------------------------------


def test_claim_conflict_takeover_release(store):
    board, _ = store.create_board("b", "p", ME)
    store.touch(board.id, ME)
    store.touch(board.id, OTHER)
    assert store.claim(board.id, OTHER, ["src/parser/*"], "/r") == []
    conflicts = store.claim(board.id, ME, ["src/parser/lexer.py", "src/ui.py"], "/r")
    assert [(p, c["agent"]) for p, c in conflicts] == [("src/parser/lexer.py", OTHER)]
    assert holders(store, board) == {(OTHER, "src/parser/*"), (ME, "src/ui.py")}

    assert store.claim(board.id, ME, ["src/parser/lexer.py"], "/other-worktree") == []  # other checkout: fine

    store.mark_left(board.id, OTHER)                       # gone: its claim can be taken over
    assert store.claim(board.id, ME, ["src/parser/*"], "/r") == []
    assert (ME, "src/parser/*") in holders(store, board)

    store.release(board.id, ME, ["src/ui.py"])
    assert (ME, "src/ui.py") not in holders(store, board)
    store.release(board.id, ME, ["*"])
    assert not any(a == ME for a, _ in holders(store, board))


def test_closing_board_clears_inventory(store):
    board, key = store.create_board("b", "p", ME)
    store.claim(board.id, ME, ["a.py"])
    store.close_board(key, ME)
    assert store.claims(board.id) == []


def test_view_shows_files_by_agent(store):
    board, key = store.create_board("b", "p", ME)
    store.post(key, ME, "rebuilding snapshot", "status")
    store.touch(board.id, ME)
    store.claim(board.id, ME, [f"src/m{i}.py" for i in range(10)])
    store.claim(board.id, OTHER, ["tests/test_api.py"])
    store.mark_left(board.id, OTHER)
    text, _ = render_view(store, board)
    assert f"Files:\n{ME}: src/m0.py, " in text and "+2 more" in text
    assert f"{OTHER} (left 0m): tests/test_api.py" in text
    assert "Now:\n" in text and "rebuilding snapshot" in text


# ---- hooks ------------------------------------------------------------------


def test_editing_claims_files_automatically(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    run("post-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)
    assert holders(store, board) == {(ME, "src/api.py")}
    n = len(store.claims(board.id))
    run("post-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)   # cached: no duplicate
    assert len(store.claims(board.id)) == n


def test_codex_patches_claim_files(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo, sid="C1", me=OTHER)
    run("post-tool", patch(["src/ui.py", "src/new.py"], repo, sid="C1"), monkeypatch, capsys, client="codex")
    assert holders(store, board) == {(OTHER, "src/ui.py"), (OTHER, "src/new.py")}


def test_edit_guard_blocks_first_edit_of_held_file(env, repo, monkeypatch, capsys):
    store, board, key = env
    top = git(repo, "rev-parse", "--show-toplevel")
    store.touch(board.id, OTHER, str(repo), top, "main")
    store.claim(board.id, OTHER, ["src/api.py"], top)
    link(board, monkeypatch, capsys, repo)
    out = run("pre-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "src/api.py held by codex-bbbb" in reason and "Edit again to proceed" in reason
    assert run("pre-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys) is None   # retry proceeds
    assert run("pre-tool", edit(repo / "src" / "ui.py", repo), monkeypatch, capsys) is None    # free file

    out = run("post-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)          # edited anyway
    assert "you edited src/api.py held by codex-bbbb" in out["hookSpecificOutput"]["additionalContext"]
    assert (OTHER, "src/api.py") in holders(store, board)                                    # not stolen


def test_commit_releases_committed_files(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    for f in ("api.py", "ui.py"):
        (repo / "src" / f).write_text("changed\n")
        run("post-tool", edit(repo / "src" / f, repo), monkeypatch, capsys)
    git(repo, "add", "src/api.py")
    git(repo, "commit", "-qm", "api done")
    run("post-tool", bash("git commit -m 'api done'", repo), monkeypatch, capsys)
    assert holders(store, board) == {(ME, "src/ui.py")}          # uncommitted work stays claimed


def test_session_end_frees_all_files(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    run("post-tool", edit(repo / "src" / "api.py", repo), monkeypatch, capsys)
    run("session-end", {"session_id": "S1"}, monkeypatch, capsys)
    assert store.claims(board.id) == []
