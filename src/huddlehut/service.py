"""Operations shared by the MCP server and the CLI."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from .distill import distill
from .store import Board, Store, env

# Who is posting. Set per client in its MCP config (e.g. "claude-code", "codex").
DEFAULT_AGENT = env("HUDDLEHUT_AGENT") or "agent"
STALE_PENDING_SECONDS = 600


def board_summary(board: Board) -> dict:
    d = board.to_dict()
    for k in ("created_at", "closed_at"):
        if d.get(k):
            d[k] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d[k]))
    return {k: v for k, v in d.items() if v not in (None, "")}


def distill_in_background(db_path: Path | str, board_id: str) -> threading.Thread:
    """Distillation takes a while; never make a client (Codex's 60s tool timeout) wait on it.
    The thread opens its own connection rather than sharing the server's."""

    def run() -> None:
        try:
            lessons = distill(Store(db_path), board_id)
            print(f"[huddlehut] distilled {len(lessons)} lessons from {board_id}", file=sys.stderr)
        except Exception as e:  # status + error are recorded on the board
            print(f"[huddlehut] distill failed for {board_id}: {e}", file=sys.stderr)

    t = threading.Thread(target=run, name=f"distill-{board_id}", daemon=True)
    t.start()
    return t


def sweep_stale_distills(store: Store) -> list[str]:
    """Pick up boards whose distill was interrupted (closing session exited before it finished)."""
    ids = store.stale_pending_boards(time.time() - STALE_PENDING_SECONDS)
    for board_id in ids:
        distill_in_background(store.url, board_id)
    return ids


_last_sweep = 0.0


def sweep_stale_sessions(store: Store, every: float = 60) -> list:
    """End sessions idle past the timeout; cheap, and at most once a minute per process."""
    global _last_sweep
    if time.time() - _last_sweep < every:
        return []
    _last_sweep = time.time()
    try:
        return store.end_stale_sessions()
    except Exception as e:  # never let housekeeping break a tool call or a page load
        print(f"[huddlehut] stale-session sweep failed: {e}", file=sys.stderr)
        return []
