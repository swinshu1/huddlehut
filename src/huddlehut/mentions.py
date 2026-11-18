"""Who a message is addressed to. One definition, shared by the hooks (who to wake or keep going), the
board server (flagging "@you", warning about names nobody answers to) and the viewer (the user's message
goes to everyone unless it names someone). Standard library only: hooks import it on every tool call."""

from __future__ import annotations

import re

EVERYONE_WORDS = ("all", "everyone", "everybody", "agents", "team")
USER_WORDS = ("user", "human")
EVERYONE = re.compile(rf"(?<![\w-])@(?:{'|'.join(EVERYONE_WORDS)})\b", re.I)
TAG = re.compile(r"(?<![\w-])@([\w-]+)")


def mention_names(me: str, others) -> set[str]:
    """How other agents may address this one: full name, 4-char tag, and the plain tool name
    ("codex", "claude-code", "claude") when no other agent on the board shares it."""
    names = {me}
    base, _, tag = me.rpartition("-")
    if base and re.fullmatch(r"[0-9a-f]{4}", tag):
        names.add(tag)
        other_bases = {o.rpartition("-")[0] for o in others if o != me}
        if base not in other_bases:
            names.add(base)
            short = base.split("-")[0]
            if short != base and short not in {b.split("-")[0] for b in other_bases}:
                names.add(short)
    return names


def mentions(text: str, names: set[str]) -> bool:
    """Whether text addresses an agent known by these names (or everyone)."""
    return bool(EVERYONE.search(text)) or names_in(text, names)


def names_in(text: str, names) -> bool:
    """Whether text names any of these (with or without the @), everyone-words aside."""
    return any(re.search(rf"(?<![\w-])@?{re.escape(n)}(?![\w-])", text, re.I) for n in names)


def tags(text: str) -> set[str]:
    """The @tags in a message, lowercased."""
    return {t.lower() for t in TAG.findall(text)}


def agent_names(store, board_id: str) -> set[str]:
    """Every way to @ an agent on this board, lowercased: nicknames, ids, tags, unique tool names."""
    from .store import USER

    labels = store.labels(board_id)
    agents = (set(store.presence(board_id)) | set(labels)) - {USER}
    names = {n.lower() for a, n in labels.items() if a != USER}
    for a in agents:
        names |= {n.lower() for n in mention_names(a, agents)}
    return names


def addressable(store, board_id: str) -> set[str]:
    """Every @tag someone on this board answers to: agents, everyone, and the user (by any name)."""
    return agent_names(store, board_id) | set(EVERYONE_WORDS) | set(USER_WORDS) | {store.user_name().lower()}


def names_agents(store, board_id: str, text: str) -> bool:
    """Whether a message @-addresses an agent on this board, or everyone."""
    return bool(tags(text) & (agent_names(store, board_id) | set(EVERYONE_WORDS)))
