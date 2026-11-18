"""Wakes an idle Codex agent when the board needs it.

Claude Code agents are woken by an asyncRewake Stop hook (`hook wait`). Codex hooks can't wake an idle
thread, so when Codex's Stop hook lets an agent stop, it starts this waiter in the background. The
waiter watches the board like `hook wait` (same rules for what needs the agent; it steps aside once
the agent works again). When something needs the agent, it resumes the agent's own thread headlessly:

    codex exec resume <thread> "<what's new, how to rejoin>"

That turn runs with the user's Codex config. Its Stop hook starts the next waiter, and its SessionEnd
hook leaves the agent on the board (the thread lives on). Its board server is a new process, so the
waiter first maps the thread to the agent's identity (HUDDLEHUT_SESSION; see server._author) and the
prompt carries the board key.

Off with HUDDLEHUT_CODEX_WAKE=0. Log: ~/.huddlehut/wake.log; the last woken turn's Codex output:
~/.huddlehut/wake/<thread>.log.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import hook

APP_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
TIMEOUT = float(os.environ.get("HUDDLEHUT_WAKE_TIMEOUT", "3600"))  # longest a woken turn may run
# the woken turn's board server must use the same database as the hooks
FORWARD = ("HUDDLEHUT_DATABASE_URL", "HUDDLEHUT_DB", "HUDDLEHUT_HOME")


def enabled() -> bool:
    return os.environ.get("HUDDLEHUT_CODEX_WAKE", "1") != "0"


def spawn(inp: dict) -> None:
    """Called by Codex's Stop hook when it lets the agent stop: start a detached waiter for the thread."""
    sid = inp.get("session_id") or ""
    if not enabled() or not hook.load(sid) or hook.awaiting_user(inp.get("last_assistant_message")):
        return  # off, not on a board, or waiting on the user: don't wake it before they answer
    cwd = inp.get("cwd") or (hook.load(sid).get("git") or {}).get("cwd") or str(Path.home())
    subprocess.Popen([sys.executable, "-m", "huddlehut.codex_wake", sid, cwd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # outlives the hook and Codex's turn


def codex_bin() -> str | None:
    # the app's own CLI first: it matches the app's thread format
    for path in (os.environ.get("HUDDLEHUT_CODEX_BIN"), APP_CODEX, shutil.which("codex")):
        if path and os.access(path, os.X_OK):
            return path
    return None


def server_name() -> str:
    """The huddlehut server's name in the user's Codex config ([mcp_servers.<name>])."""
    import tomllib

    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        servers = tomllib.loads(path.read_text()).get("mcp_servers") or {}
    except (OSError, ValueError):
        servers = {}
    for name, cfg in servers.items():
        if isinstance(cfg, dict) and "huddlehut" in json.dumps([cfg.get("command"), cfg.get("args")]):
            return name
    return "huddlehut"


def command(codex: str, sid: str, cwd: str, prompt: str) -> list[str]:
    env = {"HUDDLEHUT_SESSION": sid, **{v: os.environ[v] for v in FORWARD if os.environ.get(v)}}
    server = server_name()
    overrides = [arg for k, v in env.items() for arg in ("-c", f"mcp_servers.{server}.env.{k}={json.dumps(v)}")]
    return [codex, "exec", "--skip-git-repo-check", "-C", cwd, "resume", *overrides, sid, prompt]


def prompt_for(store, state: dict, msgs) -> str:
    board = store.get_board(state["board"])
    nick = store.nicknames(board.id).get(state["me"]) or state["me"]
    key = repr(state["key"]) if state.get("key") else "<your board key>"
    return (f"[huddlehut] You went idle; board {board.name} has something for you:\n"
            f"{hook._render(store, msgs)}\n"
            f"Your board tools restarted: join_board(key={key}, nickname={nick!r}) first (you keep your name), "
            f"then handle it on the board (reply_to). {hook.USER_QUIET}")


def log(sid: str, line: str) -> None:
    try:
        hook.HOME.mkdir(parents=True, exist_ok=True)
        with open(hook.HOME / "wake.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} {sid[:8]} {line}\n")
    except OSError:
        pass


def wake(store, sid: str, cwd: str, state: dict, msgs) -> int | str:
    """Resume the agent's thread with what needs it. Returns codex's exit code (or why it didn't run)."""
    from .store import tool_of

    if not enabled():
        return "off"
    codex = codex_bin()
    if not codex:
        log(sid, "no codex binary; set HUDDLEHUT_CODEX_BIN")
        return "no codex"
    me = state["me"]
    store.remember_identity(f"{tool_of(me)}:{sid}", me)  # the woken turn's board server is this agent
    cwd = cwd if os.path.isdir(cwd) else str(Path.home())
    cmd = command(codex, sid, cwd, prompt_for(store, state, msgs))
    state["waking_until"] = time.time() + TIMEOUT
    hook.save(sid, state)
    log(sid, f"waking {me} for {', '.join(f'#{m.id}' for m in msgs)}")
    out = hook.HOME / "wake" / f"{sid}.log"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            code = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                  timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    except OSError as e:
        code = f"{type(e).__name__}: {e}"
    finally:
        now = hook.load(sid)
        if now:
            now.pop("waking_until", None)
            hook.save(sid, now)
    log(sid, f"{me} woke, codex exit {code}")
    return code


def run(sid: str, cwd: str) -> int | str | None:
    found = hook.watch(sid)
    if not found:
        return None
    store, state, msgs = found
    return wake(store, sid, cwd, state, msgs)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sid, cwd = argv[0], argv[1]
    try:
        run(sid, cwd)
    except Exception as e:  # detached: nobody sees a traceback
        log(sid, f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
