"""Client hooks: board messages reach an agent without it spending a turn on a tool call.

One entry point for Claude Code and Codex (`--client` picks the output format):

  post-tool      after every tool call. Links the client session to a board when the agent joins or
                 creates one (from the "[b_… you=…]" marker in the result), then, at most every
                 HUDDLEHUT_HOOK_INTERVAL s: delivers others' unread messages, records presence (working
                 folder, branch), and warns once if another active agent shares this git checkout.
                 After a `git commit`, posts the new commit id so others build on a fixed point.
  pre-tool       before a Bash command that would commit, stash or discard changes: if those changes
                 touch files another active agent claimed in its status, blocks the first attempt
                 with an explanation. Running the same command again goes through.
  session-start  after compaction or resume: re-injects the board view (no model call).
  stop           before the agent ends its turn: if a question, blocker or reply to it is unread, or
                 it has an open promise it hasn't been reminded of, keeps it going once. When it lets
                 a Codex agent stop, marks the thread idle for the wake service (wake_service.py).
  session-end    marks the agent as left; if it leaves promises unkept, posts a blocker saying so,
                 so nobody waits forever.
  wait           Claude Code only, as an asyncRewake Stop hook: after the agent goes idle, waits up
                 to HUDDLEHUT_WAIT_SECONDS for something that needs it, then wakes it (exit 2).

Each link (a small file in ~/.huddlehut/sessions) belongs to one conversation: a Claude Code session, a
Codex thread, or a Codex subagent. Codex reports a subagent's events under its parent's session_id plus
the subagent's own agent_id; the subagent has its own board server and identity, so it gets its own
link, and since it fires no Stop or SessionEnd hooks, its parent's hooks end its board session once its
task finishes. Claude Code subagents share the parent's board server, so they share its link.

The read cursor is the same one the session's MCP server uses (cursors table, keyed by the agent
name), so nothing is delivered twice. Sessions that never joined a board exit before touching the
database or running git, and nothing heavy is imported, so the hook adds little to each tool call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from .mentions import agent_names, mention_names, mentions, names_in

HOME = Path(os.environ.get("HUDDLEHUT_HOME", Path.home() / ".huddlehut"))
SESSIONS = HOME / "sessions"
MARKER = re.compile(r"\[(b_[0-9a-f]{8}) you=([\w.-]+)\]")
SUBAGENT_DONE_TOOLS = re.compile(r"^collaboration.*(wait|close)")  # Codex: a parent waited on or closed a subagent
KEY = re.compile(r"\bbrd_[\w-]{16,}")
BOARD_TOOL = re.compile(r"^mcp__.*huddlehut.*__")
RISKY_GIT = re.compile(r"\bgit\b.*\b(commit|stash|reset|checkout|restore|clean)\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}  # Claude Code; Codex edits via apply_patch
INTERVAL = float(os.environ.get("HUDDLEHUT_HOOK_INTERVAL", "5"))
WAIT_SECONDS = float(os.environ.get("HUDDLEHUT_WAIT_SECONDS", "14400"))  # idle agents stay wakeable for 4h
WAIT_POLL = float(os.environ.get("HUDDLEHUT_WAIT_POLL", "10"))
WAIT_GRACE = 3  # let the synchronous Stop hook claim anything already unread
GIT_INFO_TTL = 60
MAX_LINES = 15
MAX_CHARS = 4000  # under Codex's ~2.5K-token and Claude Code's 10K-char hook output caps
# Appended where an agent is about to talk to the user (stop, wake): board traffic belongs on the board.
USER_QUIET = "The user doesn't see the board; tell them only if they need to act or decide."
# Ends a wake. The woken turn's reply lands in the user's chat although the user asked nothing: one word,
# unless the user really is needed.
WOKEN = ("You were woken by the board, not by the user: handle it on the board. Your reply here shows up in "
         "the user's chat, so unless they need to act or decide, reply with just: ok")


# ---- per-client-session link state (a small local file) ---------------------


def _state_path(session_id: str) -> Path:
    return SESSIONS / (re.sub(r"[^\w.-]", "_", session_id) + ".json")


def load(session_id: str) -> dict:
    try:
        return json.loads(_state_path(session_id).read_text())
    except (OSError, ValueError):
        return {}


def save(session_id: str, state: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    path = _state_path(session_id)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def unlink(session_id: str) -> None:
    _state_path(session_id).unlink(missing_ok=True)


def _thread(inp: dict, client: str) -> tuple[str, str | None]:
    """The conversation a hook event belongs to: (its link key, its parent session or None)."""
    sid = inp.get("session_id") or ""
    if client == "codex" and inp.get("agent_id"):
        return str(inp["agent_id"]), sid  # a subagent: its own thread and board identity
    return sid, None


# ---- git --------------------------------------------------------------------


def _git(cwd: str | None, *args: str) -> str | None:
    import subprocess  # lazy: most hook calls never need it

    if not cwd:
        return None
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _git_info(state: dict, cwd: str | None) -> dict:
    """Toplevel and branch of the agent's working folder, cached for GIT_INFO_TTL seconds."""
    cached = state.get("git") or {}
    if cached.get("cwd") == cwd and time.time() - cached.get("at", 0) < GIT_INFO_TTL:
        return cached
    top = _git(cwd, "rev-parse", "--show-toplevel")
    info = {"cwd": cwd, "at": time.time(), "toplevel": top,
            "branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") if top else None}
    state["git"] = info
    return info


def _head(cwd: str | None) -> tuple[str, str, float, str] | None:
    out = _git(cwd, "log", "-1", "--format=%H%x09%h%x09%ct%x09%s")
    if not out:
        return None
    full, short, ts, subject = (out.split("\t", 3) + ["", "", "0", ""])[:4]
    return full, short, float(ts or 0), subject


def _changed_files(cwd: str, staged_only: bool) -> set[str]:
    def lines(*args):
        return {ln for ln in (_git(cwd, *args) or "").splitlines() if ln}

    files = lines("diff", "--cached", "--name-only")
    if not staged_only:
        files |= lines("diff", "--name-only") | lines("ls-files", "--others", "--exclude-standard")
    return files


def _risky_git(command: str) -> tuple[str, bool, str | None] | None:
    """(verb for the warning, staged_only, -C dir) if the command would commit, stash or discard
    changes; None for safe commands. Stash/reset/checkout/restore/clean hit the whole tree."""
    import shlex

    try:  # punctuation_chars splits out && ; | while keeping quoted text (commit messages) whole
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    segments, cur = [], []
    for tok in tokens:
        if tok and set(tok) <= set("&|;"):
            segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    segments.append(cur)
    adds_all = False
    for t in segments:
        if "git" not in t:
            continue
        i, cdir = t.index("git") + 1, None
        while i < len(t) and t[i].startswith("-"):  # global options: -C dir, -c k=v
            if t[i] in ("-C", "-c") and i + 1 < len(t):
                cdir = t[i + 1] if t[i] == "-C" else cdir
                i += 2
            else:
                i += 1
        if i >= len(t):
            continue
        sub, rest = t[i], t[i + 1:]
        if sub == "add" and any(a in ("-A", "--all", ".", "-u", "--update") for a in rest):
            adds_all = True
        elif sub == "commit":
            all_flag = adds_all or any(a in ("-a", "--all") or re.fullmatch(r"-\w*a\w*", a) for a in rest)
            return "commit", not all_flag, cdir
        elif sub == "stash" and (not rest or rest[0] in ("push", "save", "-u", "--include-untracked", "-a")):
            return "stash away", False, cdir
        elif sub == "reset" and "--hard" in rest:
            return "discard", False, cdir
        elif sub in ("checkout", "restore") and "." in rest and "--staged" not in rest:
            return "discard", False, cdir
        elif sub == "clean" and any(re.fullmatch(r"-\w*f\w*", a) for a in rest):
            return "delete", False, cdir
    return None


def _edited_paths(tool: str, tool_input: dict) -> list[str]:
    if tool in EDIT_TOOLS:
        p = tool_input.get("file_path") or tool_input.get("notebook_path")
        return [p] if p else []
    if tool == "apply_patch":
        from .claims import patch_paths

        return patch_paths(str(tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or ""))
    return []


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _holders(store, board_id: str, conflicts) -> list[str]:
    """'src/a.py, src/b.py held by codex-3f2a (active 2m ago)' per holding agent."""
    from .compact import _age

    seen, by_agent, names = store.presence(board_id), {}, store.labels(board_id)
    for path, c in conflicts:
        by_agent.setdefault(c["agent"], []).append(path)
    now = time.time()
    return [
        f"{', '.join(paths[:5])}{' …' if len(paths) > 5 else ''} held by {names.get(agent, agent)}"
        f" (active {_age(now - (seen.get(agent) or {}).get('seen_at', now))} ago)"
        for agent, paths in by_agent.items()
    ]


# ---- message selection and rendering ----------------------------------------


def _render(store, msgs) -> str:
    from .compact import render_entries

    omitted = max(0, len(msgs) - MAX_LINES)
    text = render_entries(store, msgs[omitted:])
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:].split("\n", 1)[-1]
    if omitted:
        text = f"(+{omitted} earlier: read_messages(since_id={msgs[0].id - 1}))\n" + text
    return text


def _needs_me(store, board_id: str, me: str, msgs) -> list:
    """Unread messages that call for this agent: ones that mention it (or everyone), replies to its posts,
    and questions or blockers that name no agent (anyone may answer). A question for someone else isn't
    this agent's to answer, so it doesn't wake it or keep it from stopping."""
    if not msgs:
        return []
    mine = {r["id"] for r in store._all("SELECT id FROM messages WHERE board_id=? AND author=?", (board_id, me))}
    names = mention_names(me, set(store.presence(board_id)) | {m.author for m in msgs})
    nick = store.nicknames(board_id).get(me)
    if nick:
        names.add(nick)
    others = agent_names(store, board_id) - {n.lower() for n in names}

    def for_me(m) -> bool:
        if m.reply_to in mine or mentions(m.content, names):
            return True
        return m.kind in ("question", "blocker") and not names_in(m.content, others)

    return [m for m in msgs if for_me(m)]


def _open_promises(store, board_id: str, me: str) -> list:
    return [m for m in store.open_items(board_id) if m.kind == "promise" and m.author == me]


def _take_unread(store, state: dict):
    """Unread messages from others; advances the shared cursor past them."""
    msgs = store.unread(state["board"], state["me"])
    if msgs:
        store.set_cursor(state["board"], state["me"], msgs[-1].id)
    return msgs


# An agent whose last reply waits on the user can't act on board traffic anyway; holding it from
# stopping or waking it would just make it chat in the user's window. Its next turn gets the news.
def awaiting_user(last_reply: str | None) -> bool:
    if not last_reply:
        return False
    tail = re.sub(r"[\s*_`>)\].]+$", "", last_reply.strip()[-400:])
    from .store import ASKS_USER

    return tail.endswith("?") or bool(ASKS_USER.search(tail))


def _store():
    from .store import Store

    return Store()


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _snip(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---- leaving: one path, and subagents that never say goodbye ------------------


def _leave(key: str, state: dict, reason: str) -> None:
    """This conversation's board session is over. Departure itself lives in the store (end_session),
    shared with the viewer's end button and the stale-session sweep."""
    try:
        _store().end_session(state["board"], state["me"], by=state["me"], reason=reason, announce=False)
    finally:
        unlink(key)


def thread_running(transcript: str | None, limit: int = 8 << 20) -> bool | None:
    """Whether a Codex thread is mid-task, from its transcript (the rollout JSONL whose path Codex gives
    hooks): True after a task_started, False after task_complete or turn_aborted, None if unknown.
    Reads backwards in chunks (a busy turn can write megabytes after its task_started), up to limit."""
    if not transcript:
        return None
    try:
        with open(transcript, "rb") as f:
            end = f.seek(0, os.SEEK_END)
            pos, carry = end, b""
            while pos > 0 and end - pos < limit:
                step = min(1 << 16, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + carry
                lines = chunk.split(b"\n")
                carry = lines.pop(0) if pos > 0 else b""  # a line cut by the chunk edge waits for the next chunk
                for line in reversed(lines):
                    kind = _task_event(line)
                    if kind is not None:
                        return kind
    except OSError:
        return None
    return None


def _task_event(line: bytes) -> bool | None:
    if b'"task_' not in line and b'"turn_aborted"' not in line:
        return None
    try:
        kind = (json.loads(line).get("payload") or {}).get("type")
    except (ValueError, AttributeError):
        return None
    if kind == "task_started":
        return True
    if kind in ("task_complete", "turn_aborted"):
        return False
    return None


def subagents_of(parent: str) -> list[tuple[str, dict]]:
    """Links of the subagents this session spawned: (link key, state)."""
    if not parent or not SESSIONS.is_dir():
        return []
    found = []
    for path in SESSIONS.glob("*.json"):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if state.get("parent") == parent:
            found.append((path.stem, state))
    return found


def end_subagents(parent: str, everyone: bool = False) -> list[str]:
    """End the board sessions of this session's subagents whose task finished, or of all of them when
    the parent's own session ends. Returns the agents ended."""
    ended = []
    for key, state in subagents_of(parent):
        if everyone or thread_running(state.get("transcript")) is False:
            _leave(key, state, "ended (subagent finished)")
            ended.append(state["me"])
    return ended


def _seen(state: dict, inp: dict, client: str, now: float) -> None:
    """The conversation just did something: when, which client, and its transcript (the wake service
    reads it to tell an idle Codex thread from a busy one)."""
    state["active"] = now
    state["client"] = client
    if inp.get("transcript_path"):
        state["transcript"] = inp["transcript_path"]


def _went_idle(inp: dict) -> None:
    """A Codex thread ended its turn: mark it idle (and whether it waits on the user) and make sure
    the wake service runs, so the board can wake it later."""
    sid = inp.get("session_id") or ""
    state = load(sid)
    if not state:
        return
    state |= {"client": "codex", "stopped_at": time.time(),
              "awaiting_user": awaiting_user(inp.get("last_assistant_message"))}
    if inp.get("cwd"):
        state["cwd"] = inp["cwd"]
    if inp.get("transcript_path"):
        state["transcript"] = inp["transcript_path"]
    save(sid, state)
    from . import wake_service

    wake_service.ensure()


# ---- events -----------------------------------------------------------------


def post_tool(inp: dict, client: str) -> dict | None:
    sid, parent = _thread(inp, client)
    tool = inp.get("tool_name") or ""
    cwd = inp.get("cwd")
    now = time.time()
    if client == "codex" and not parent and SUBAGENT_DONE_TOOLS.match(tool):
        end_subagents(sid)  # it waited on or closed its subagents: end the ones that finished
    if BOARD_TOOL.match(tool):
        response = json.dumps(inp.get("tool_response"), ensure_ascii=False)
        m = MARKER.search(response)
        state = load(sid)
        if m:
            head = _head(cwd)
            # rejoining the same board keeps what the hooks track; a Codex wake in progress belongs to the thread
            same = state.get("board") == m.group(1) and state.get("me") == m.group(2)
            keep = state if same else {k: v for k, v in state.items() if k == "waking_until"}
            state = {**keep, "board": m.group(1), "me": m.group(2), "checked": 0,
                     "last_sha": head[0] if head else None}
            # kept so a Codex wake can tell the agent how to rejoin (the database stores keys hashed)
            key = KEY.search(str((inp.get("tool_input") or {}).get("key", ""))) or KEY.search(response)
            if key:
                state["key"] = key.group(0)
            if parent:
                state["parent"] = parent  # its parent's hooks end it
        if state:
            _seen(state, inp, client, now)
            save(sid, state)
        return None  # the board tool's own result already carried any new messages
    state = load(sid)
    if not state:
        return None  # not on a board: no database access, no git
    _seen(state, inp, client, now)
    command = str((inp.get("tool_input") or {}).get("command", "")) if tool == "Bash" else ""
    committed = "git" in command and "commit" in command
    edited = _edited_paths(tool, inp.get("tool_input") or {})
    fresh = []
    if edited:
        from .claims import normalize

        top = _git_info(state, cwd).get("toplevel")
        mine = set(state.get("mine", []))
        # only files inside the repo are shared work; scratch files elsewhere stay private
        fresh = [p for p in dict.fromkeys(normalize(e, top, cwd) for e in edited)
                 if p not in mine and not os.path.isabs(p)]
    if not committed and not fresh and now - state.get("checked", 0) < INTERVAL:
        save(sid, state)
        return None
    state["checked"] = now
    store = _store()
    board = store.get_board(state["board"])
    notes = []

    if fresh and board.status == "open":  # editing a file claims it: the inventory fills itself in
        top = (state.get("git") or {}).get("toplevel")
        conflicts = store.claim(board.id, state["me"], fresh, top)
        held = {p for p, _ in conflicts}
        state["mine"] = sorted(set(state.get("mine", [])) | (set(fresh) - held))
        if conflicts:
            notes += [f"you edited {line}. Tell them on the board." for line in _holders(store, board.id, conflicts)]

    if committed and board.status == "open":
        head = _head(cwd)
        if head and head[0] != state.get("last_sha") and head[2] >= now - 300:
            state["last_sha"] = head[0]
            branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "?"
            store.post_to(board, state["me"], f"committed {head[1]} ({branch}): {_snip(head[3], 80)}", "result")
            done = [f for f in (_git(cwd, "show", "--name-only", "--format=", "HEAD") or "").splitlines() if f]
            if done:  # committed work is checkpointed: free those files for others
                store.release(board.id, state["me"], done)
                state["mine"] = sorted(set(state.get("mine", [])) - set(done))

    info = _git_info(state, cwd)
    store.touch(board.id, state["me"], cwd, info.get("toplevel"), info.get("branch"))
    store.set_waiting(board.id, state["me"], None)  # working again: whatever it waited on was answered
    if board.status == "open" and not store.nicknames(board.id).get(state["me"]):
        # sessions that joined before nicknames existed: give them one and say so, once
        nick, _ = store.set_nickname(board.id, state["me"])
        notes.append(f"you're {nick} on this board now; others can @{nick} you.")
    if inp.get("model") and state.get("model") != inp["model"]:  # Codex reports the real model on every hook
        store.set_model(board.id, state["me"], inp["model"])
        state["model"] = inp["model"]
    if info.get("toplevel"):
        warned = set(state.get("warned_shared", []))
        for agent, p in store.live_agents(board.id, exclude=state["me"]).items():
            if p.get("toplevel") == info["toplevel"] and agent not in warned:
                warned.add(agent)
                repo = Path(info["toplevel"]).name
                notes.append(
                    f"{store.labels(board.id).get(agent, agent)} works in the same checkout ({info['toplevel']}). Uncommitted edits from both "
                    f"of you mix there. Safer: git worktree add ../{repo}-{state['me']} -b {state['me']}"
                )
        state["warned_shared"] = sorted(warned)

    msgs = _take_unread(store, state)
    save(sid, state)
    if board.status != "open":
        unlink(sid)
        extra = "\n" + _render(store, msgs) if msgs else ""
        return _context("PostToolUse", f"[huddlehut] board {board.name} was closed.{extra}")
    parts = [f"[huddlehut {board.name}] new from others:\n" + _render(store, msgs)] if msgs else []
    parts += [f"[huddlehut] {n}" for n in notes]
    return _context("PostToolUse", "\n".join(parts)) if parts else None


def pre_tool(inp: dict, client: str) -> dict | None:
    """Speed bumps: editing a file another active agent holds, or committing/discarding files
    another active agent holds. The first attempt is blocked with the reason; retrying proceeds."""
    tool = inp.get("tool_name") or ""
    tool_input = inp.get("tool_input") or {}
    if tool in EDIT_TOOLS or tool == "apply_patch":
        return _guard_edit(inp, tool, tool_input, client)
    if tool != "Bash":
        return None
    command = str(tool_input.get("command", ""))
    if not RISKY_GIT.search(command):
        return None
    sid, _ = _thread(inp, client)
    state = load(sid)
    if not state:
        return None
    risky = _risky_git(command)
    if not risky:
        return None
    verb, staged_only, cdir = risky
    cwd = inp.get("cwd") or "."
    gdir = os.path.join(cwd, cdir) if cdir else cwd
    toplevel = _git(gdir, "rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    files = _changed_files(gdir, staged_only)
    if not files:
        return None
    store = _store()
    conflicts = store.conflicts(state["board"], state["me"], sorted(files), toplevel)
    if not conflicts:
        return None
    locked = [(p, c) for p, c in conflicts if c.get("locked")]
    if locked:  # a lock is a hard stop until its holder unlocks, leaves, or goes idle
        return _deny(
            f"[huddlehut] This would {verb} locked files:\n" + "\n".join(_holders(store, state["board"], locked))
            + "\nAsk them to unlock (files(unlock=[...])), or leave those files out (git commit <paths>)."
        )
    digest = hashlib.sha1(command.encode()).hexdigest()[:12]
    if digest in state.get("guard_passed", []):
        return None  # second attempt: the agent has seen the warning and chose to proceed
    state.setdefault("guard_passed", []).append(digest)
    save(sid, state)
    return _deny(
        f"[huddlehut] This would {verb} files another agent is working on:\n"
        + "\n".join(_holders(store, state["board"], conflicts))
        + "\nLikely their unfinished work. Commit only your own files (git commit <paths>) or ask on the "
        "board first. Run the same command again to proceed anyway."
    )


def _guard_edit(inp: dict, tool: str, tool_input: dict, client: str) -> dict | None:
    sid, _ = _thread(inp, client)
    state = load(sid)
    if not state:
        return None
    paths = _edited_paths(tool, tool_input)
    if not paths:
        return None
    from .claims import normalize

    cwd = inp.get("cwd")
    top = _git_info(state, cwd).get("toplevel")
    mine, passed = set(state.get("mine", [])), set(state.get("guard_passed", []))
    todo = [p for p in dict.fromkeys(normalize(x, top, cwd) for x in paths) if p not in mine and not os.path.isabs(p)]
    if not todo:
        save(sid, state)
        return None  # own files: no database needed
    store = _store()
    conflicts = store.conflicts(state["board"], state["me"], todo, top)
    locked = [(p, c) for p, c in conflicts if c.get("locked")]
    soft = [(p, c) for p, c in conflicts if not c.get("locked") and f"edit:{p}" not in passed]
    if soft:
        state["guard_passed"] = sorted(passed | {f"edit:{p}" for p, _ in soft})
    save(sid, state)
    if locked:  # hard stop until the holder unlocks, leaves, or goes idle
        return _deny(
            "[huddlehut] Locked: " + "; ".join(_holders(store, state["board"], locked))
            + ". Ask them to unlock on the board, or pick other work."
        )
    if not soft:
        return None
    return _deny(
        "[huddlehut] " + "; ".join(_holders(store, state["board"], soft))
        + ". They're probably mid-edit. Ask on the board or pick other work. Edit again to proceed anyway."
    )


def session_start(inp: dict, client: str) -> dict | None:
    source = inp.get("source")
    sid = inp.get("session_id") or ""
    state = load(sid)
    if client == "codex" and state:
        from . import wake_service

        wake_service.ensure()  # after a reboot or app restart, the first linked thread brings it back
    if source not in ("compact", "resume") or not state:
        return None
    from .compact import render_view

    store = _store()
    board = store.get_board(state["board"])
    if board.status != "open":
        unlink(sid)
        return None
    text, last_id = render_view(store, board)
    store.set_cursor(board.id, state["me"], last_id)
    tail = "" if source == "compact" else "\nSession resumed: join_board(key) again before posting."
    return _context("SessionStart", f"[huddlehut] you are {state['me']} on this board:\n{text}{tail}")


def stop(inp: dict, client: str) -> dict | None:
    if client == "codex":
        end_subagents(inp.get("session_id") or "")
    out = _stop(inp, client)
    if out is None and client == "codex":
        _went_idle(inp)  # Codex can't be rewoken by a hook; the wake service resumes it instead
    return out


def _stop(inp: dict, client: str) -> dict | None:
    if inp.get("stop_hook_active"):
        return None  # already continued once for this stop; don't loop
    sid = inp.get("session_id") or ""
    state = load(sid)
    if not state:
        return None
    store = _store()
    last = inp.get("last_assistant_message")
    if awaiting_user(last):
        # waiting on the user: pin it in the viewer, let it stop; board news arrives on its next turn
        store.set_waiting(state["board"], state["me"], last)
        return None
    store.set_waiting(state["board"], state["me"], None)
    msgs = store.unread(state["board"], state["me"])
    urgent = _needs_me(store, state["board"], state["me"], msgs)
    reminded = set(state.get("reminded", []))
    promises = [m for m in _open_promises(store, state["board"], state["me"]) if m.id not in reminded]
    if not urgent and not promises:
        return None
    parts = []
    if urgent:
        store.set_cursor(state["board"], state["me"], msgs[-1].id)
        parts.append("[huddlehut] unread, needs you before stopping:\n" + _render(store, msgs))
    if promises:
        state["reminded"] = sorted(reminded | {m.id for m in promises})
        save(sid, state)
        parts.append(
            "[huddlehut] open promises you made; others may be waiting:\n"
            + "\n".join(f"#{m.id}: {_snip(m.content, 100)}" for m in promises)
            + "\nKeep one with post(..., reply_to=<id>), or post an update if it's delayed."
        )
    text = "\n".join(parts + [USER_QUIET])
    if client == "codex":
        return {"decision": "block", "reason": text}
    return _context("Stop", text)


def session_end(inp: dict, client: str) -> dict | None:
    sid = inp.get("session_id") or ""
    end_subagents(sid, everyone=True)  # they don't outlive the session that spawned them
    state = load(sid)
    if not state or state.get("waking_until", 0) > time.time():
        return None  # not on a board, or a headless wake turn ended: the agent's own thread lives on
    _leave(sid, state, "ended")
    return None


def wait(inp: dict, client: str) -> int:
    """Background waiter (Claude Code asyncRewake): exit 2 + stderr wakes the idle agent."""
    sid = inp.get("session_id") or ""
    state = load(sid)
    if not state or awaiting_user(inp.get("last_assistant_message")):
        return 0  # not on a board, or waiting on the user: don't wake it before they answer
    found = watch(sid)
    if not found:
        return 0
    store, _, msgs = found
    print("[huddlehut] new on the board for you:\n" + _render(store, msgs) + "\n" + WOKEN, file=sys.stderr)
    return 2


def watch(sid: str):
    """After a linked session goes idle, wait up to WAIT_SECONDS for something that needs it.
    Returns (store, state, msgs) with the cursor moved past msgs, or None if the agent started working
    again (post-tool delivers then), a newer watcher took over, or the board closed."""
    state = load(sid)
    if not state:
        return None
    token, started = uuid.uuid4().hex, time.time()
    state["waiter"] = token
    save(sid, state)
    time.sleep(WAIT_GRACE)
    store = _store()
    while time.time() - started < WAIT_SECONDS:
        state = load(sid)
        if not state or state.get("waiter") != token or state.get("active", 0) > started:
            return None
        msgs = store.unread(state["board"], state["me"])
        if _needs_me(store, state["board"], state["me"], msgs):
            store.set_cursor(state["board"], state["me"], msgs[-1].id)
            return store, state, msgs
        if store.get_board(state["board"]).status != "open":
            return None
        time.sleep(WAIT_POLL)
    return None


EVENTS = {"post-tool": post_tool, "pre-tool": pre_tool, "session-start": session_start, "stop": stop,
          "session-end": session_end}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    client = argv[argv.index("--client") + 1] if "--client" in argv else "claude"
    try:
        inp = json.load(sys.stdin)
    except ValueError:
        return 0
    if os.environ.get("HUDDLEHUT_HOOK_TRACE"):  # debugging: what each client sends its hooks
        try:
            with open(os.environ["HUDDLEHUT_HOOK_TRACE"], "a") as f:
                f.write(json.dumps({"event": event, "client": client, "at": time.time(), "input": inp}) + "\n")
        except OSError:
            pass
    try:
        if event == "wait":
            return wait(inp, client)
        handler = EVENTS.get(event)
        out = handler(inp, client) if handler else None
        if out:
            print(json.dumps(out))
    except Exception as e:  # a hook must never break the agent's session
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            with open(HOME / "hook.log", "a") as f:
                f.write(f"{time.strftime('%F %T')} {event}: {type(e).__name__}: {e}\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
