"""Whether a board message can reach an agent, and whether the agent is answering.

From the database alone, so every host sharing a board sees the same thing:

  working          acted on the board in the last WORKING_WITHIN seconds
  idle             not acting, but its hooks are linked, so a mention wakes it
  unlinked         its hooks never ran here (no working folder recorded): mentions reach it only when it
                   next uses a board tool, and nothing can wake it
  not responding   something that needs it has waited over STUCK_AFTER seconds and it hasn't acted since:
                   its session may be stuck, errored or gone

The viewer shows the last two on the agent, and an agent that @-names one is told not to wait on it.
"""

from __future__ import annotations

import time

from .mentions import mention_names, needs, tags
from .store import USER

WORKING_WITHIN = 120
STUCK_AFTER = 600


def reach_of(store, board_id: str, agent: str, p: dict, now: float | None = None) -> dict:
    """{"status", "linked", "waiting" (how many messages need it), "since" (the oldest one's time)}."""
    now = now or time.time()
    waiting = needs(store, board_id, agent, store.unread(board_id, agent))
    since = waiting[0].created_at if waiting else None
    linked = bool(p.get("cwd"))
    if since and now - since > STUCK_AFTER and p["seen_at"] < since:
        status = "not responding"
    elif now - p["seen_at"] <= WORKING_WITHIN:
        status = "working"
    elif not linked:
        status = "unlinked"
    else:
        status = "idle"
    return {"status": status, "linked": linked, "waiting": len(waiting), "since": since}


def reach(store, board_id: str) -> dict[str, dict]:
    """reach_of for every agent still on the board."""
    now = time.time()
    return {a: reach_of(store, board_id, a, p, now) for a, p in store.presence(board_id).items()
            if a != USER and p["state"] != "left"}


def _age(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h"


def unreachable_note(store, board_id: str, content: str, author: str) -> str:
    """For a post that @-names agents a mention can't reach right now: one line each, so its author
    doesn't wait on them. Posts to everyone don't count; they aren't waiting on anyone in particular."""
    tagged = tags(content)
    if not tagged:
        return ""
    presence = store.presence(board_id)
    nicks = store.nicknames(board_id)
    notes = []
    for agent, p in presence.items():
        if agent in (author, USER) or p["state"] == "left":
            continue
        names = mention_names(agent, set(presence)) | ({nicks[agent]} if nicks.get(agent) else set())
        if not tagged & {n.lower() for n in names}:  # an explicit @, not a name in passing
            continue
        r = reach_of(store, board_id, agent, p)
        name = nicks.get(agent) or agent
        if r["status"] == "not responding":
            notes.append(f"{name} hasn't acted on the board for {_age(time.time() - p['seen_at'])} and "
                         f"{r['waiting']} message(s) wait for it; its session may be stuck. Don't wait on it.")
        elif r["status"] == "unlinked":
            notes.append(f"{name}'s hooks aren't linked, so nothing wakes it: it sees this only when it next "
                         f"uses a board tool.")
    return "".join("\n" + n for n in notes)
