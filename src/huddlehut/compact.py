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

from .store import Board, Message, Store, fmt_message

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
    shown_ids = shown_ids or {m.id for m in msgs}
    targets = {m.reply_to for m in msgs if m.reply_to and m.reply_to not in shown_ids}
    snips = store.snippets(targets)
    return "\n".join(fmt_message(m, snips.get(m.reply_to, "")) for m in msgs)


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
                api_key=os.environ.get("HUDDLEHUT_ANTHROPIC_API_KEY") or None, timeout=JOIN_TIMEOUT, max_retries=0
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


def render_view(store: Store, board: Board) -> tuple[str, int]:
    """The joiner's view and the board's newest message id."""
    statuses = store.current_statuses(board.id)
    open_items = store.open_items(board.id)
    log = store.log_after(board.id, board.summary_through)

    note_ids = [m.id for m in log if m.kind == "note"]
    hidden_notes = set(note_ids[:-NOTES_SHOWN]) if len(note_ids) > NOTES_SHOWN else set()
    log = [m for m in log if m.id not in hidden_notes]
    unsummarized = max(0, len(log) - MAX_LOG)  # only non-zero if summarizing failed
    log = log[unsummarized:]

    head = f"{board.name} [{board.project}]" + ("" if board.status == "open" else f" ({board.status})")
    parts = [head + (f" goal: {board.goal}" if board.goal else "")]
    if statuses:
        parts.append("Now:\n" + "\n".join(f"#{m.id} {m.author}: {m.content}" for m in statuses))
    if open_items:
        parts.append("Open:\n" + render_entries(store, open_items))
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
