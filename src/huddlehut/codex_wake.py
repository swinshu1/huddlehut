"""Wakes an idle Codex agent by queueing what needs it on the board to its own thread.

The wake service (wake_service.py) decides when; this module delivers:

    codex queue --thread <thread> --message "<what's new>" --resume

The message goes into the thread's queue (Codex's own way to message an existing session). A thread that
something has open, such as the Codex app or the app-server daemon, is its "active writer", which takes the
message as a new turn right away. So the agent answers in its own session, with its own board server
(same identity), and the user sees the turn in the app. A thread nobody has open is resumed on the local
app-server daemon (--resume), with the daemon's own config, so that turn gets a fresh board server: the
message carries the board key and a one-time wake ticket, and join_board(ticket=...) gets the agent its
identity back. (Config overrides can't help: the daemon ignores them, and `codex queue` refuses them
while a daemon runs.)

(Resuming with `codex exec resume` instead fails on any thread the app has open: one writer per thread.)

Off with HUDDLEHUT_CODEX_WAKE=0. Log: ~/.huddlehut/wake.log; the last wake's Codex output:
~/.huddlehut/wake/<thread>.log.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import hook

APP_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
TIMEOUT = float(os.environ.get("HUDDLEHUT_WAKE_TIMEOUT", "120"))  # longest queueing (and a daemon resume) may take
GUARD = 30  # seconds after queueing that the thread counts as being woken; then its transcript says it's busy


def enabled() -> bool:
    return os.environ.get("HUDDLEHUT_CODEX_WAKE", "1") != "0"


def codex_bin() -> str | None:
    # the app's own CLI first: it matches the app's thread format
    for path in (os.environ.get("HUDDLEHUT_CODEX_BIN"), APP_CODEX, shutil.which("codex")):
        if path and os.access(path, os.X_OK):
            return path
    return None


def command(codex: str, sid: str, prompt: str) -> list[str]:
    return [codex, "queue", "--thread", sid, "--message", prompt, "--resume"]


def prompt_for(store, state: dict, msgs, ticket: str) -> str:
    board = store.get_board(state["board"])
    nick = store.nicknames(board.id).get(state["me"]) or state["me"]
    key = repr(state["key"]) if state.get("key") else "<your board key>"
    return (f"[huddlehut] You were idle; board {board.name} has something for you:\n"
            f"{hook._render(store, msgs)}\n"
            f"First join_board(key={key}, nickname={nick!r}, ticket={ticket!r}): it keeps you {nick} even if "
            f"your board tools restarted. Then handle it on the board (post with reply_to). {hook.USER_QUIET}")


def log(sid: str, line: str) -> None:
    try:
        hook.HOME.mkdir(parents=True, exist_ok=True)
        with open(hook.HOME / "wake.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} {sid[:8]} {line}\n")
    except OSError:
        pass


def wake(store, sid: str, cwd: str, state: dict, msgs) -> int | str:
    """Queue what needs the agent to its thread. Returns codex's exit code (or why it didn't run). The
    turn itself runs in whatever has the thread open, so this returns once the message is queued."""
    if not enabled():
        return "off"
    codex = codex_bin()
    if not codex:
        log(sid, "no codex binary; set HUDDLEHUT_CODEX_BIN")
        return "no codex"
    me = state["me"]
    cwd = cwd if os.path.isdir(cwd) else str(Path.home())
    cmd = command(codex, sid, prompt_for(store, state, msgs, store.issue_ticket(state["board"], me)))
    state["waking_until"] = time.time() + GUARD  # lapses on its own: the turn runs elsewhere
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
    log(sid, f"queued for {me}, codex exit {code}")
    return code
