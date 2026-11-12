"""Turn a closed board's transcript into reusable, scalable problem-solving lessons."""

from __future__ import annotations

import os

import anthropic
from pydantic import BaseModel, Field

from .compact import render_entries
from .store import Board, Message, Store, env

MODEL = os.environ.get("HUDDLEHUT_MODEL", "claude-opus-5-5")
SPARSE_SIGNAL_CHARS = 1500  # below this, include notes too
MAX_TRANSCRIPT_CHARS = 600_000  # ~150K tokens; well inside the 1M window


class Lesson(BaseModel):
    title: str = Field(description="Short imperative name for the practice, e.g. 'Bisect flaky tests by seed'")
    technique: str = Field(description="Concrete steps someone else can follow. Caveman-terse fragments, under 60 words.")
    when_to_apply: str = Field(description="Situation or symptoms that signal it applies. Terse, under 20 words.")
    tags: list[str] = Field(description="3-6 lowercase keywords for retrieval (tools, domains, failure modes)")


class Distillation(BaseModel):
    lessons: list[Lesson]


SYSTEM = """You review the shared message board of a finished multi-agent project and extract \
lessons worth remembering for future, unrelated projects.

Keep a lesson only if it is:
- transferable: it would help an agent on a different codebase or task, not just this one
- scalable: a repeatable practice, coordination pattern, debugging method or design choice, \
not a one-off fact (file paths, IDs, specific values belong nowhere in the output)
- earned: the transcript shows it worked, or shows the costly mistake it avoids

Include coordination lessons (how the agents split work, handed off, avoided duplicate effort) \
when the transcript shows them. Prefer 3-8 strong lessons; return an empty list if nothing \
qualifies rather than padding. Lessons are retrieved into other agents' contexts later, so write \
them caveman-terse: no articles or filler, no made-up abbreviations, but complete enough to act on."""


def signal_messages(store: Store, board_id: str) -> list[Message]:
    """Everything that carries a lesson: decisions, results, dead ends, Q&A, blockers and each agent's
    final status. Notes and superseded statuses are chatter and are left out to save tokens."""
    final_status = {m.id for m in store.current_statuses(board_id)}
    msgs = [m for m in store.all_messages(board_id) if m.kind != "status" or m.id in final_status]
    signal = [m for m in msgs if m.kind != "note"]
    # Agents that never typed their posts leave everything as notes; then notes are the signal.
    if sum(len(m.content) for m in signal) < SPARSE_SIGNAL_CHARS:
        return msgs
    return signal


def _render(store: Store, board: Board, messages: list[Message]) -> str:
    head = (
        f"Project: {board.project}\nBoard: {board.name}\nGoal: {board.goal or '-'}\n"
        f"Outcome at close: {board.outcome or '-'}\n\nLog:\n"
    )
    text = head + render_entries(store, messages)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise ValueError(
            f"log is {len(text):,} chars, over the {MAX_TRANSCRIPT_CHARS:,} limit; "
            "distill it in parts instead of truncating"
        )
    return text


def distill(store: Store, board_id: str, client: anthropic.Anthropic | None = None) -> list[dict]:
    """Distill a closed board into memories. Records status on the board so failures can be retried."""
    board = store.get_board(board_id)
    messages = signal_messages(store, board_id)
    if not messages:
        store.set_distill_status(board_id, "done")
        return []
    try:
        client = client or anthropic.Anthropic(api_key=env("HUDDLEHUT_ANTHROPIC_API_KEY"))
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=SYSTEM,
            messages=[{"role": "user", "content": _render(store, board, messages)}],
            output_format=Distillation,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model declined to distill: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(f"no structured output (stop_reason={response.stop_reason})")
        lessons = [l.model_dump() for l in response.parsed_output.lessons]
        store.add_memories(board_id, board.project, lessons)
        store.set_distill_status(board_id, "done")
        return lessons
    except Exception as e:
        store.set_distill_status(board_id, "failed", f"{type(e).__name__}: {e}")
        raise
