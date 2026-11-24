"""What a joining agent reads, built mostly without a model.

Message kinds have lifecycles (see store.MESSAGE_KINDS), so the board summarizes itself:
  Now    newest `status` per agent (older statuses are superseded)
  Open   questions/blockers nobody has replied to
  Log    decisions, results, dead ends, answers; only the newest few notes
A model is called only when the durable log after the last summary outgrows COMPACT_AFTER_CHARS,
and only when someone joins. Then it folds the oldest entries into a short summary
(previous summary + folded entries -> new summary). Most boards never trigger it.
"""

from __future__ import annotations

import os
from pathlib import Path

import time

from .store import IDLE_AFTER, USER, Board, Message, Store, env, fmt_message, short_model

MODEL = os.environ.get("HUDDLEHUT_COMPACT_MODEL", os.environ.get("HUDDLEHUT_MODEL", "claude-opus-5-5"))
COMPACT_AFTER_CHARS = int(os.environ.get("HUDDLEHUT_COMPACT_AFTER_CHARS", "6000"))  # ~1.5K tokens
KEEP_RAW = 6        # newest log entries always shown verbatim
NOTES_SHOWN = 3     # older notes are chatter: hidden from the view (still folded into summaries)
MAX_LOG = 20        # verbatim cap if summarizing fails
JOIN_TIMEOUT = 40   # seconds; stays under Codex's default 60s tool timeout

SYSTEM = """You compress the older part of a coordination log shared by AI agents in separate \
sessions on one project. Their current statuses and open questions are shown separately; cover only \
background a newcomer needs: decisions + why, done, dead ends (so nobody retries), resolved questions, \
constraints.

Merge new entries into previous summary. Keep what's still true, drop superseded. Caveman style: \
fragments under short headings, no articles or filler, no made-up abbreviations. Keep paths, \
commands, errors, numbers, negations. Attribute decisions to agents. Under 150 words. Output only \
the summary."""


def render_entries(store: Store, msgs: list[Message], shown_ids: set[int] | None = None) -> str:
    """Lines for messages; a reply to something not shown alongside it gets a snippet of the target."""
    if not msgs:
        return ""
    shown_ids = shown_ids or {m.id for m in msgs}
    targets = {m.reply_to for m in msgs if m.reply_to and m.reply_to not in shown_ids}
    snips = store.snippets(targets)
    names = store.labels(msgs[0].board_id)
    if names.get(USER, USER) != USER:
        names[USER] += " (user)"  # so agents know a named post came from the user
    return "\n".join(fmt_message(m, snips.get(m.reply_to, ""), names.get(m.author)) for m in msgs)


def _log_chars(log: list[Message]) -> int:
    return sum(len(m.content) for m in log)


def needs_compaction(store: Store, board: Board) -> bool:
    log = store.log_after(board.id, board.summary_through)
    return len(log) > KEEP_RAW and _log_chars(log) >= COMPACT_AFTER_CHARS


def compact(store: Store, board_id: str, client=None, force: bool = False) -> bool:
    """Fold all but the newest KEEP_RAW log entries into the summary. Returns True if it ran.

    force: ignore the cooldown left by a failed attempt (used by the CLI's manual compact)."""
    if force:
        store.release_compaction(board_id)
    if not store.claim_compaction(board_id):
        return False  # another session is on it, or a recent attempt failed (cooldown)
    try:
        board = store.get_board(board_id)
        log = store.log_after(board_id, board.summary_through)
        to_fold = log[:-KEEP_RAW] if len(log) > KEEP_RAW else []
        if not to_fold:
            store.release_compaction(board_id)
            return False
        if client is None:
            import anthropic  # imported lazily: hooks render views without paying for the SDK import

            client = anthropic.Anthropic(
                api_key=env("HUDDLEHUT_ANTHROPIC_API_KEY"), timeout=JOIN_TIMEOUT, max_retries=0
            )
        prompt = (
            f"Project: {board.project}\nGoal: {board.goal or '-'}\n\n"
            f"<previous_summary>\n{board.summary or '(none)'}\n</previous_summary>\n\n"
            f"<new_entries>\n{render_entries(store, to_fold)}\n</new_entries>"
        )
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model declined to summarize: {response.stop_details}")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"empty summary (stop_reason={response.stop_reason})")
        store.save_summary(board_id, text, to_fold[-1].id)
        return True
    except Exception as e:
        store.release_compaction(board_id, f"{type(e).__name__}: {e}")
        raise


def _age(seconds: float) -> str:
    return f"{int(seconds // 60)}m" if seconds < 5400 else f"{int(seconds // 3600)}h"


def presence_tag(p: dict | None, now: float | None = None) -> str:
    """' (left 5m)' / ' (idle 40m)' / '' so readers know whether an owner is still around."""
    if not p:
        return ""
    now = now or time.time()
    if p["state"] == "left":
        return f" (left {_age(now - p['seen_at'])})"
    if now - p["seen_at"] > IDLE_AFTER:
        return f" (idle {_age(now - p['seen_at'])})"
    return ""


def render_files(store: Store, board_id: str, per_agent: int = 8) -> str:
    """The shared inventory, one line per agent: who is working on which files."""
    claims = store.claims(board_id)
    if not claims:
        return ""
    seen = store.presence(board_id)
    names = store.labels(board_id)
    by_agent: dict[str, list[str]] = {}
    for c in claims:
        by_agent.setdefault(c["agent"], []).append(c["path"] + (" (locked)" if c.get("locked") else ""))
    lines = [
        f"{names.get(agent, agent)}{presence_tag(seen.get(agent))}: {', '.join(paths[:per_agent])}"
        + (f" +{len(paths) - per_agent} more" if len(paths) > per_agent else "")
        for agent, paths in by_agent.items()
    ]
    return "Files:\n" + "\n".join(lines)


def render_progress(store: Store, board_id: str) -> str:
    """Milestones toward the board's goal: "Progress 1/3, todo: m7 lexer spans; m9 parser tests"."""
    ms = store.milestones(board_id)
    if not ms:
        return ""
    todo = [m for m in ms if not m["done_at"]]
    rest = ("todo: " + "; ".join(f"m{m['id']} {m['text']}" for m in todo)) if todo else "all milestones done"
    return f"Progress {len(ms) - len(todo)}/{len(ms)}, {rest}"


def render_view(store: Store, board: Board) -> tuple[str, int]:
    """The joiner's view and the board's newest message id."""
    statuses = store.current_statuses(board.id)
    open_items = store.open_items(board.id)
    seen = store.presence(board.id)
    names = store.labels(board.id)

    def label(agent: str) -> str:  # "mochi (opus 5.5)": the model shows in the roster only
        model = short_model((seen.get(agent) or {}).get("model"))
        return names.get(agent, agent) + (f" ({model})" if model else "")

    log = store.log_after(board.id, board.summary_through)

    note_ids = [m.id for m in log if m.kind == "note"]
    hidden_notes = set(note_ids[:-NOTES_SHOWN]) if len(note_ids) > NOTES_SHOWN else set()
    log = [m for m in log if m.id not in hidden_notes]
    unsummarized = max(0, len(log) - MAX_LOG)  # only non-zero if summarizing failed
    log = log[unsummarized:]

    head = f"{board.name} [{board.project}]" + ("" if board.status == "open" else f" ({board.status})")
    parts = [head + (f" goal: {board.goal}" if board.goal else "")]
    progress = render_progress(store, board.id)
    if progress:
        parts.append(progress)
    here = [label(a) + presence_tag(p) for a, p in sorted(seen.items(), key=lambda kv: -kv[1]["seen_at"])
            if p["state"] != "left"]
    if here:
        parts.append("Huddlers: " + ", ".join(here))
    user = store.user_name()
    if user != "user":
        parts.append(f"User: {user} (@{user} or @user)")
    statuses = [m for m in statuses if (seen.get(m.author) or {}).get("state") != "left"]  # gone: not "now"
    if statuses:
        parts.append("Now:\n" + "\n".join(
            f"#{m.id} {label(m.author)}{presence_tag(seen.get(m.author))}: {m.content}" for m in statuses))
    inventory = render_files(store, board.id)
    if inventory:
        parts.append(inventory)
    if open_items:
        lines = render_entries(store, open_items).split("\n")
        # flag open promises whose author is gone: someone else has to pick them up
        lines = [
            ln.replace(f" {names.get(m.author, m.author)}/",
                       f" {names.get(m.author, m.author)}{presence_tag(seen.get(m.author))}/", 1)
            if m.kind == "promise" else ln
            for ln, m in zip(lines, open_items)
        ]
        parts.append("Open:\n" + "\n".join(lines))
    if board.summary:
        parts.append("Earlier:\n" + board.summary)
    if log:
        shown = {m.id for m in log} | {m.id for m in open_items}
        parts.append("Log:\n" + render_entries(store, log, shown))
    if hidden_notes or unsummarized:
        bits = [f"{len(hidden_notes)} older notes hidden"] if hidden_notes else []
        if unsummarized:
            bits.append(f"{unsummarized} older log entries not yet summarized")
        parts.append(f"({'; '.join(bits)}; read_messages(since_id={board.summary_through}) shows all)")
    last_id = store.max_message_id(board.id)
    parts.append(f"last_id={last_id}")
    return "\n".join(parts), last_id


def join_board(db_path: Path | str, key: str, client=None) -> tuple[str, int]:
    """Fold the log if it has outgrown the threshold, then render the view.
    Opens its own connection so it can run in a worker thread."""
    store = Store(db_path)
    board = store.board_for_key(key)
    if board.status == "open" and needs_compaction(store, board):
        try:
            compact(store, board.id, client)
        except Exception:
            pass  # recorded as compact_error; the view falls back to a capped verbatim log
        board = store.get_board(board.id)
    return render_view(store, board)
