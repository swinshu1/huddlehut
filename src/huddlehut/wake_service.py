"""One wake service per machine for idle Codex agents.

Claude Code wakes an idle agent with its asyncRewake Stop hook. Codex hooks can't wake an idle thread,
so this process does it from outside. The registry is the hook links already on disk
(~/.huddlehut/sessions: which thread is which agent, its board, key and transcript). Every POLL seconds
the service checks each linked Codex thread for unread board messages that need its agent, and if the
thread is idle it resumes it with `codex exec resume` (codex_wake.wake).

Idle means not mid-turn: the transcript's latest task event is a completion (hook.thread_running), or,
without a readable transcript, its Stop hook ran after its last tool call. A thread whose last reply
waits on the user isn't woken (as with Claude Code), and neither is a subagent: its parent owns it.

Exactly one runs per machine: it holds an exclusive lock on ~/.huddlehut/wake.lock. Codex's Stop and
SessionStart hooks and the viewer call ensure(), which starts it if nobody holds the lock. It exits
after LINGER seconds with no Codex thread to watch. Off with HUDDLEHUT_CODEX_WAKE=0.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

from . import codex_wake, hook

POLL = float(os.environ.get("HUDDLEHUT_WAKE_POLL", "5"))
LINGER = float(os.environ.get("HUDDLEHUT_WAKE_LINGER", "900"))
CONCURRENCY = max(1, int(os.environ.get("HUDDLEHUT_WAKE_CONCURRENCY", "2")))


def _lock():
    """The service lock, held (an open file), or None if another process holds it."""
    hook.HOME.mkdir(parents=True, exist_ok=True)
    f = open(hook.HOME / "wake.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def running() -> bool:
    f = _lock()
    if f is None:
        return True
    f.close()  # closing releases it
    return False


def ensure() -> bool:
    """Start the service unless it's running or waking is off. Cheap: one lock attempt.
    Returns whether it started one."""
    if not codex_wake.enabled() or running():
        return False
    subprocess.Popen([sys.executable, "-m", "huddlehut.wake_service"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def codex_threads() -> list[tuple[str, dict]]:
    """Hook links of Codex threads that can be woken: (link key, state). Subagents are their parent's."""
    found = []
    for path in sorted(hook.SESSIONS.glob("*.json")) if hook.SESSIONS.is_dir() else []:
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        codex = state.get("client") == "codex" or (not state.get("client") and str(state.get("me", "")).startswith("codex"))
        if codex and not state.get("parent") and state.get("board") and state.get("me"):
            found.append((path.stem, state))
    return found


def idle(state: dict) -> bool:
    running = hook.thread_running(state.get("transcript"))
    if running is not None:
        return not running
    return 0 < state.get("active", 0) <= state.get("stopped_at", 0)  # no transcript: go by the Stop hook


def due(store, links, busy=frozenset()) -> list[tuple[str, dict, list]]:
    """The threads to wake now, with the messages that need them."""
    from .store import BoardError

    out, now = [], time.time()
    for key, state in links:
        if key in busy or state.get("waking_until", 0) > now or state.get("awaiting_user"):
            continue
        try:
            board = store.get_board(state["board"])
        except BoardError:
            continue
        if board.status != "open" or (store.presence(board.id).get(state["me"]) or {}).get("state") == "left":
            continue
        msgs = store.unread(board.id, state["me"])
        if hook._needs_me(store, board.id, state["me"], msgs) and idle(state):  # cheap checks first
            out.append((key, state, msgs))
    return out


def cwd_of(state: dict) -> str:
    return state.get("cwd") or (state.get("git") or {}).get("cwd") or str(Path.home())


def _job(key: str, state: dict, msgs, busy: set) -> None:
    from .store import Store

    try:
        codex_wake.wake(Store(), key, cwd_of(state), state, msgs)  # its own connection: it runs in a worker
    except Exception as e:
        codex_wake.log(key, f"{type(e).__name__}: {e}")
    finally:
        busy.discard(key)


def tick(store, busy: set, pool: Executor) -> list[str]:
    """One look at every linked Codex thread; wakes the ones that are due. Returns their link keys."""
    started = []
    for key, state, msgs in due(store, codex_threads(), busy):
        store.set_cursor(state["board"], state["me"], msgs[-1].id)  # delivered: the wake prompt carries them
        busy.add(key)
        pool.submit(_job, key, state, msgs, busy)
        started.append(key)
    return started


def serve() -> int:
    lock = _lock()
    if lock is None:
        return 0  # another one is running
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    from .store import Store

    store, busy = Store(), set()
    codex_wake.log("service", f"started, pid {os.getpid()}")
    quiet_since = time.time()
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        while True:
            try:
                if codex_threads():
                    quiet_since = time.time()
                elif time.time() - quiet_since > LINGER:
                    break
                tick(store, busy, pool)
            except Exception as e:  # keep serving: one bad link or a database hiccup isn't fatal
                codex_wake.log("service", f"{type(e).__name__}: {e}")
            time.sleep(POLL)
    codex_wake.log("service", "stopped: no Codex threads to watch")
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
