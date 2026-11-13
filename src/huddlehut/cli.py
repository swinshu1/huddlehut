"""Command-line access to the same boards, for humans and for agents that prefer shell over MCP.

The key can be passed with --key or the HUDDLEHUT_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .compact import compact, join_board
from .distill import distill
from .service import DEFAULT_AGENT, board_summary
from .store import MESSAGE_KINDS, BoardError, Store, fmt_messages


def _print_msgs(msgs) -> None:
    print(fmt_messages(msgs))


def _distill(store: Store, board_id: str) -> int:
    try:
        lessons = distill(store, board_id)
    except Exception as e:
        print(f"distillation failed: {type(e).__name__}: {e}\nretry with: huddlehut distill {board_id}", file=sys.stderr)
        return 2
    print(f"{len(lessons)} lessons saved to memory:")
    for l in lessons:
        print(f"- {l['title']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="huddlehut", description=__doc__)
    p.add_argument("--agent", default=DEFAULT_AGENT, help="author name (default: $HUDDLEHUT_AGENT)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def keyed(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--key", default=os.environ.get("HUDDLEHUT_KEY"), help="board key (or $HUDDLEHUT_KEY)")
        return sp

    c = sub.add_parser("create", help="spin up a board")
    c.add_argument("name")
    c.add_argument("--project", required=True)
    c.add_argument("--goal", default="")

    keyed("join", help="show the compacted summary plus recent messages (what a joining agent sees)")
    keyed("compact", help="fold older messages into the board summary now")

    r = keyed("read", help="read the full message history")
    r.add_argument("--since", type=int, default=0)

    po = keyed("post", help="post a message")
    po.add_argument("content")
    po.add_argument("--kind", default="note", choices=MESSAGE_KINDS)
    po.add_argument("--reply-to", type=int, default=None, help="id of the question/blocker this answers")

    w = keyed("wait", help="block until a new message arrives")
    w.add_argument("--since", type=int, required=True)
    w.add_argument("--timeout", type=float, default=300)

    ls = sub.add_parser("list", help="list boards (no keys)")
    ls.add_argument("--project")
    ls.add_argument("--all", action="store_true", help="include closed boards")

    cl = keyed("close", help="spin down a board and distill its lessons (waits for distillation)")
    cl.add_argument("--outcome", default="")

    keyed("reopen", help="spin a closed board back up")
    keyed("status", help="show board status")

    s = sub.add_parser("search", help="search distilled memories")
    s.add_argument("query")
    s.add_argument("--project")
    s.add_argument("--limit", type=int, default=8)

    d = sub.add_parser("distill", help="(re)run distillation for a closed board by id, e.g. after a failure")
    d.add_argument("board_id")

    cf = sub.add_parser("config", help="show or set where boards are stored")
    cf.add_argument("--database-url", help="postgresql://... for a shared database; '' for local SQLite")

    ins = sub.add_parser("install", help="wire huddlehut into Claude Code (plugin) or Codex (MCP + hooks)")
    ins.add_argument("client", choices=("claude", "codex"))
    ins.add_argument("--dry-run", action="store_true", help="print what would change")

    v = sub.add_parser("view", help="open the board viewer in your browser (boards, transcripts, memory)")
    v.add_argument("--port", type=int, default=8787)
    v.add_argument("--host", default="127.0.0.1", help="keep the default unless you mean to share it")
    v.add_argument("--no-open", action="store_true", help="don't open a browser")

    a = p.parse_args(argv)
    if a.cmd == "view":
        from .viewer import serve

        serve(a.host, a.port, open_browser=not a.no_open)
        return 0
    if a.cmd in ("config", "install"):
        from . import install

        if a.cmd == "config":
            print(install.set_database(a.database_url) if a.database_url is not None else install.describe())
            return 0
        try:
            steps = (install.install_claude if a.client == "claude" else install.install_codex)(a.dry_run)
        except (RuntimeError, OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(("Would run:\n" if a.dry_run else "Done:\n") + "\n".join(f"  {s}" for s in steps))
        return 0
    store = Store()
    if hasattr(a, "key") and not a.key:
        p.error("a board key is required: pass --key or set HUDDLEHUT_KEY")

    try:
        if a.cmd == "create":
            board, key = store.create_board(a.name, a.project, a.agent, a.goal)
            print(f"board {board.id} created\nkey: {key}\n(share the key with the other sessions; it can't be recovered)")
        elif a.cmd == "join":
            print(join_board(store.path, a.key)[0])
        elif a.cmd == "compact":
            board = store.board_for_key(a.key)
            try:
                ran = compact(store, board.id, force=True)
            except Exception as e:
                print(f"compaction failed: {type(e).__name__}: {e}", file=sys.stderr)
                return 2
            print("summary updated" if ran else "nothing to compact (or another session is compacting)")
        elif a.cmd == "read":
            board, msgs = store.read(a.key, a.since, 10_000)
            print(f"# {board.name}  [{board.project}]  status={board.status}")
            _print_msgs(msgs)
        elif a.cmd == "post":
            m = store.post(a.key, a.agent, a.content, a.kind, a.reply_to)
            print(f"posted #{m.id}")
        elif a.cmd == "wait":
            board, msgs = store.wait_for_messages(a.key, a.since, a.timeout)
            _print_msgs(msgs) if msgs else print("(no new messages)")
        elif a.cmd == "list":
            for b in store.list_boards(a.project, None if a.all else "open"):
                print(f"{b.id}  {b.status:6}  {b.project}  —  {b.name}  (by {b.created_by})")
        elif a.cmd == "close":
            if a.outcome:
                store.post(a.key, a.agent, a.outcome, kind="result")
            board = store.close_board(a.key, a.agent, a.outcome)
            print(f"board {board.id} closed; distilling lessons...")
            return _distill(store, board.id)
        elif a.cmd == "reopen":
            board = store.reopen_board(a.key, a.agent)
            print(f"board {board.id} reopened")
        elif a.cmd == "status":
            print(json.dumps(board_summary(store.board_for_key(a.key)), indent=2))
        elif a.cmd == "search":
            for hit in store.search_memories(a.query, a.project, a.limit):
                print(f"## {hit['title']}  [{hit['project']}]\n{hit['technique']}\nWhen: {hit['when_to_apply']}\n")
        elif a.cmd == "distill":
            board = store.get_board(a.board_id)
            if board.status != "closed":
                raise BoardError("only closed boards are distilled")
            return _distill(store, board.id)
    except BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
