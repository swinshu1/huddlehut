"""Promises, presence, commit posts, shared-checkout warning and the commit guard."""

import io
import json
import subprocess
import time

import pytest

from huddlehut import hook
from huddlehut.compact import render_view
from huddlehut.hook import _risky_git

ME, OTHER = "claude-code-aaaa", "codex-bbbb"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "src" / "api.py").write_text("x = 1\n")
    (r / "src" / "ui.py").write_text("y = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def env(db_url, store, tmp_path, monkeypatch):
    monkeypatch.setenv("HUDDLEHUT_DATABASE_URL", db_url)
    monkeypatch.setattr(hook, "HOME", tmp_path / "home")
    monkeypatch.setattr(hook, "SESSIONS", tmp_path / "home" / "sessions")
    monkeypatch.setattr(hook, "INTERVAL", 0)
    board, key = store.create_board("b", "p", ME)
    return store, board, key


def run(event, payload, monkeypatch, capsys, client="claude"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hook.main([event, "--client", client])
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def link(board, monkeypatch, capsys, cwd, sid="S1", me=ME):
    resp = [{"type": "text", "text": f"[{board.id} you={me}]\nb [p]\nlast_id=0"}]
    run("post-tool", {"session_id": sid, "cwd": str(cwd), "tool_name": "mcp__huddlehut__join_board",
                      "tool_response": resp}, monkeypatch, capsys)


def bash(cmd, cwd, sid="S1"):
    return {"session_id": sid, "cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": cmd},
            "tool_response": {"stdout": ""}}


# ---- promises ---------------------------------------------------------------


def test_promise_stays_open_until_kept(store):
    _, key = store.create_board("b", "p", ME)
    pr = store.post(key, OTHER, "post when SECURITY_SEEDS rebuilt", "promise")
    assert store.post(key, OTHER, "x", "will").kind == "promise"   # alias
    assert pr.id in {m.id for m in store.open_items(pr.board_id)}
    store.post(key, OTHER, "rebuilt; seeds at data/seeds.json", "result", reply_to=pr.id)
    assert pr.id not in {m.id for m in store.open_items(pr.board_id)}


def test_view_marks_idle_and_departed_owners(store):
    board, key = store.create_board("b", "p", ME)
    store.post(key, OTHER, "rebuilding snapshot; own data/*", "status")
    store.post(key, OTHER, "post when rebuilt", "promise")
    store.post(key, ME, "tests; own tests/*", "status")
    store.touch(board.id, ME)
    store.mark_left(board.id, OTHER)
    text, _ = render_view(store, board)
    assert f"{OTHER} (left 0m): rebuilding snapshot" in text
    assert f"{OTHER} (left 0m)/promise: post when rebuilt" in text
    assert f"{ME}: tests" in text                                      # active: no tag
    store.db.execute("UPDATE presence SET seen_at=? WHERE agent=?", (time.time() - 3600, ME))
    assert f"{ME} (idle 60m)" in render_view(store, board)[0]


def test_stop_reminds_about_own_promise_once(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    pr = store.post(key, ME, "post when snapshot rebuilt", "promise")
    out = run("stop", {"session_id": "S1"}, monkeypatch, capsys)
    assert f"#{pr.id}: post when snapshot rebuilt" in out["hookSpecificOutput"]["additionalContext"]
    assert run("stop", {"session_id": "S1"}, monkeypatch, capsys) is None   # not nagged every stop


def test_session_end_marks_left_and_flags_unkept_promises(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    pr = store.post(key, ME, "post when snapshot rebuilt", "promise")
    run("session-end", {"session_id": "S1", "reason": "other"}, monkeypatch, capsys)
    assert store.presence(board.id)[ME]["state"] == "left"
    blocker = store.unread(board.id, OTHER)[-1]
    assert blocker.kind == "blocker" and f"#{pr.id}" in blocker.content and "Unkept promise" in blocker.content
    assert hook.load("S1") == {}


def test_session_end_quiet_without_promises(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    run("session-end", {"session_id": "S1"}, monkeypatch, capsys)
    assert store.unread(board.id, OTHER) == []


# ---- commits and shared checkouts -------------------------------------------


def test_commit_is_posted_with_its_id(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    (repo / "src" / "api.py").write_text("x = 2\n")
    git(repo, "commit", "-qam", "snapshot rebuild")
    sha = git(repo, "rev-parse", "--short", "HEAD")
    run("post-tool", bash("git commit -am 'snapshot rebuild'", repo), monkeypatch, capsys)
    post = store.unread(board.id, OTHER)[-1]
    assert post.kind == "result" and post.author == ME
    assert post.content == f"committed {sha} (main): snapshot rebuild"
    run("post-tool", bash("git commit -m again", repo), monkeypatch, capsys)  # no new commit
    assert len(store.unread(board.id, OTHER)) == 1


def test_shared_checkout_warned_once(env, repo, monkeypatch, capsys):
    store, board, key = env
    store.touch(board.id, OTHER, str(repo), git(repo, "rev-parse", "--show-toplevel"), "main")
    link(board, monkeypatch, capsys, repo)
    out = run("post-tool", bash("ls", repo), monkeypatch, capsys)
    note = out["hookSpecificOutput"]["additionalContext"]
    assert f"{OTHER} works in the same checkout" in note and "git worktree add" in note
    assert run("post-tool", bash("ls", repo), monkeypatch, capsys) is None


# ---- commit guard -----------------------------------------------------------


def claim(store, board, key, repo, agent=OTHER, paths="src/api.py"):
    store.post(key, agent, f"rebuilding routes; own {paths}", "status")
    store.touch(board.id, agent, str(repo), git(repo, "rev-parse", "--show-toplevel"), "main")


def test_guard_blocks_first_commit_of_claimed_files_then_lets_it_through(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    claim(store, board, key, repo)
    (repo / "src" / "api.py").write_text("x = 3\n")    # the other agent's unfinished edit
    cmd = "git add -A && git commit -m 'my tests'"
    out = run("pre-tool", bash(cmd, repo), monkeypatch, capsys)
    dec = out["hookSpecificOutput"]
    assert dec["hookEventName"] == "PreToolUse" and dec["permissionDecision"] == "deny"
    assert "src/api.py claimed by codex-bbbb" in dec["permissionDecisionReason"]
    assert run("pre-tool", bash(cmd, repo), monkeypatch, capsys) is None     # same command again: proceeds


def test_guard_ignores_own_files_and_other_worktrees(env, repo, tmp_path, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    claim(store, board, key, repo)
    (repo / "src" / "ui.py").write_text("y = 2\n")     # not claimed by anyone else
    git(repo, "add", "src/ui.py")
    assert run("pre-tool", bash("git commit -m ui", repo), monkeypatch, capsys) is None

    (repo / "src" / "api.py").write_text("x = 9\n")
    store.touch(board.id, OTHER, str(tmp_path / "wt"), str(tmp_path / "wt"), "other")  # it moved to a worktree
    assert run("pre-tool", bash("git commit -am all", repo), monkeypatch, capsys) is None


def test_guard_catches_stash_and_hard_reset(env, repo, monkeypatch, capsys):
    store, board, key = env
    link(board, monkeypatch, capsys, repo)
    claim(store, board, key, repo)
    (repo / "src" / "api.py").write_text("x = 4\n")
    for cmd in ("git stash", "git reset --hard", "git checkout -- ."):
        out = run("pre-tool", bash(cmd, repo), monkeypatch, capsys)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_guard_skips_unlinked_sessions_and_safe_commands(env, repo, monkeypatch, capsys):
    store, board, key = env
    assert run("pre-tool", bash("git commit -am x", repo, sid="nobody"), monkeypatch, capsys) is None
    link(board, monkeypatch, capsys, repo)
    claim(store, board, key, repo)
    (repo / "src" / "api.py").write_text("x = 5\n")
    for cmd in ("git status", "git diff", "git log", "git stash list", "git commit --amend -m x"):
        assert run("pre-tool", bash(cmd, repo), monkeypatch, capsys) is None, cmd


@pytest.mark.parametrize("cmd,expected", [
    ("git commit -m 'add -a flag'", ("commit", True, None)),
    ("git commit -am msg", ("commit", False, None)),
    ("git add . && git commit -m x", ("commit", False, None)),
    ("git -C sub commit -m x", ("commit", True, "sub")),
    ("git stash push -u", ("stash away", False, None)),
    ("git reset --hard HEAD~1", ("discard", False, None)),
    ("git restore --staged .", None),
    ("git clean -fd", ("delete", False, None)),
    ("git push && git log", None),
])
def test_risky_git_parsing(cmd, expected):
    assert _risky_git(cmd) == expected
