"""Storage for boards, messages, read cursors and distilled memories.

Two backends behind one API:
- SQLite (default): a local file shared by every agent process on this machine. Zero setup.
- Postgres (set a postgresql:// URL): a shared hosted database (Supabase, Neon, RDS, ...), so agents
  on different machines and different people's sessions can share boards.

SQL is written once with `?` placeholders and `INSERT ... RETURNING`, which both backends support;
only the schema and full-text search differ.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home() / ".huddlehut"
DEFAULT_SQLITE = HOME / "board.db"
CONFIG_FILE = HOME / "config.json"


def env(name: str) -> str | None:
    """An env var's value, ignoring empty values and unresolved "${...}" placeholders (a plugin
    option the user never set can arrive either way)."""
    v = os.environ.get(name, "").strip()
    return v if v and not v.startswith("${") else None


def database_url() -> str:
    """Env first (HUDDLEHUT_DATABASE_URL; HUDDLEHUT_DB for a SQLite path; the Claude Code plugin's
    database_url option), then ~/.huddlehut/config.json, then the local SQLite file."""
    for var in ("HUDDLEHUT_DATABASE_URL", "HUDDLEHUT_DB", "CLAUDE_PLUGIN_OPTION_DATABASE_URL"):
        if env(var):
            return env(var)
    try:
        url = json.loads(CONFIG_FILE.read_text()).get("database_url")
        if url:
            return url
    except (OSError, ValueError):
        pass
    return str(DEFAULT_SQLITE)


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


MAX_POST_CHARS = 1000
# Each kind has a lifecycle that lets the board summarize itself without a model call:
#   status    one live slot per agent; a new status replaces the old one in views
#   question, blocker, promise   shown until any message replies to them (reply_to);
#             a promise is something the author owes others ("will post when X is rebuilt")
#   decision, result, deadend, answer   durable: kept (and eventually summarized)
#   note      chatter: only the newest few are shown
MESSAGE_KINDS = ("status", "decision", "question", "answer", "blocker", "promise", "result", "deadend", "note")
OPEN_KINDS = ("question", "blocker", "promise")
DURABLE_KINDS = ("decision", "result", "deadend", "answer")
# Agents guess kind names; mapping beats an error round trip (which costs a whole extra turn).
KIND_ALIASES = {"update": "note", "thinking": "note", "plan": "status", "progress": "status",
                "handoff": "note", "info": "note", "done": "result", "dead_end": "deadend",
                "failed": "deadend", "reply": "answer", "todo": "promise", "will": "promise",
                "commitment": "promise", "owe": "promise"}
IDLE_AFTER = 900  # seconds without activity before an agent shows as idle
TICKET_TTL = 3600  # how long a wake ticket stays good
# The board server's feature level. A server process keeps the code it started with, often for days (the
# Codex app never restarts it), so a change that running sessions only get by restarting raises this:
# servers record their level as they act, and the viewer flags agents on an older one.
# 1: goal tool, entering a board on any tool use, wake tickets, reach notes, milestone reminders.
# 2: one agent per conversation: Codex names its thread on every call, and the Codex app shares one server.
SERVER_LEVEL = 2
SESSION_TIMEOUT = float(os.environ.get("HUDDLEHUT_SESSION_TIMEOUT_DAYS", "3")) * 86400  # then it's ended

CUTE_NAMES = ("mochi", "pebble", "sprout", "biscuit", "dumpling", "noodle", "pickle", "waffle", "acorn", "button",
              "pudding", "peanut", "clover", "maple", "tofu", "fig", "nugget", "bean", "muffin", "sesame", "truffle",
              "pumpkin", "marble", "puffin", "otter", "bramble", "cocoa", "juniper", "kiwi", "toffee", "pretzel", "minnow")
USER = "user"  # author id of everything the human posts (from the viewer, or as the user)
RESERVED_NICKNAMES = {"all", "everyone", "everybody", "agents", "team", "agent", "user", "me", "you",
                      "codex", "claude", "claude-code"}


ASKS_USER = re.compile(
    r"\b(?:waiting (?:on|for) (?:you|your)|your call\b|(?:need|awaiting|want|waiting for) your (?:answer|go-ahead|ok|decision)|"
    r"let me know|when you(?:'re| are) ready|"
    r"up to you|should i\b|want me to\b|do you want\b|would you like\b|which (?:one|option|do you)\b|"
    r"(?:just )?say [\"“'][^\"”']{1,30}[\"”']|^\W*say \w+|when you (?:want|need|decide|get a chance))",
    re.I,
)


def ask_tail(reply: str, limit: int = 500) -> str:
    """The part of a reply that asks the user something: its last paragraph (with the one before it
    when the last is very short), trimmed at a sentence boundary rather than mid-word."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", reply.strip()) if p.strip()]
    tail = paras[-1] if paras else reply.strip()
    if len(tail) < 80 and len(paras) > 1 and len(paras[-2]) <= 240:  # short ask: add a short lead-in
        tail = paras[-2] + "\n\n" + tail
    if len(tail) > limit:
        cut = tail[-limit:]
        m = re.search(r"(?<=[.!?:])\s+", cut)
        tail = cut[m.end():] if m and m.end() < len(cut) // 2 else "…" + cut.split(" ", 1)[-1]
    # keep just the sentences that ask; the context around them is for the session, not the pin
    sentences = re.split(r"(?<=[.!?])\s+", tail.replace("\n", " "))
    asks = [x for x in sentences if x.rstrip(" *_`").endswith("?") or ASKS_USER.search(x)]
    return " ".join(asks) if asks else tail


def short_id(agent: str) -> str:
    base, _, tag = agent.rpartition("-")
    return tag if base and re.fullmatch(r"[0-9a-f]{4}", tag) else agent


def _snip(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def short_model(model: str | None) -> str:
    """claude-opus-5-5 -> opus 5.5, claude-sonnet-5-5 -> sonnet 5.5; others as given."""
    if not model:
        return ""
    m = re.sub(r"^(?:anthropic[./])?claude-", "", model.strip().lower())
    m = re.sub(r"-(\d+)-(\d+)(?:-\d{8})?$", r" \1.\2", m)
    return re.sub(r"-(\d+)$", r" \1", m) if m != model.lower() else model


def tool_of(agent: str) -> str:
    """The client an agent id came from: "claude-code-3f2a" -> "claude-code"."""
    base, _, tag = agent.rpartition("-")
    return base if base and re.fullmatch(r"[0-9a-f]{4}", tag) else agent


def default_nickname(agent: str, taken=()) -> str:
    """A cute name picked from the agent id, skipping names already used on the board."""
    start = int(hashlib.sha1(agent.encode()).hexdigest(), 16) % len(CUTE_NAMES)
    for i in range(len(CUTE_NAMES)):
        name = CUTE_NAMES[(start + i) % len(CUTE_NAMES)]
        if name not in taken:
            return name
    n = 2
    while f"{CUTE_NAMES[start]}{n}" in taken:
        n += 1
    return f"{CUTE_NAMES[start]}{n}"

_BOARD_COLS = """
    id            TEXT PRIMARY KEY,
    key_hash      TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    project       TEXT NOT NULL,
    goal          TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL,
    created_at    {real} NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    closed_by     TEXT,
    closed_at     {real},
    outcome       TEXT,
    distill_status TEXT,
    distill_error TEXT,
    summary       TEXT,
    summary_through BIGINT NOT NULL DEFAULT 0,
    compact_lock  {real},
    compact_error TEXT
"""

SQLITE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS boards ({_BOARD_COLS.format(real="REAL")});
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id   TEXT NOT NULL REFERENCES boards(id),
    author     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL,
    reply_to   INTEGER
);
CREATE INDEX IF NOT EXISTS messages_board ON messages(board_id, id);
CREATE TABLE IF NOT EXISTS cursors (
    board_id TEXT NOT NULL, reader TEXT NOT NULL, last_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (board_id, reader)
);
CREATE TABLE IF NOT EXISTS presence (
    board_id TEXT NOT NULL, agent TEXT NOT NULL, seen_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'active', cwd TEXT, toplevel TEXT, branch TEXT, nickname TEXT, model TEXT,
    waiting TEXT, waiting_at REAL, level INTEGER,
    PRIMARY KEY (board_id, agent)
);
CREATE TABLE IF NOT EXISTS claims (
    board_id TEXT NOT NULL, toplevel TEXT NOT NULL DEFAULT '', path TEXT NOT NULL, agent TEXT NOT NULL,
    since REAL NOT NULL, locked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (board_id, toplevel, path)
);
CREATE TABLE IF NOT EXISTS identities (
    client TEXT PRIMARY KEY, agent TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wake_tickets (
    token TEXT PRIMARY KEY, board_id TEXT NOT NULL, agent TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT, board_id TEXT NOT NULL REFERENCES boards(id), text TEXT NOT NULL,
    position REAL NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL, done_by TEXT, done_at REAL
);
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id      TEXT NOT NULL REFERENCES boards(id),
    project       TEXT NOT NULL,
    title         TEXT NOT NULL,
    technique     TEXT NOT NULL,
    when_to_apply TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title, technique, when_to_apply, tags, project,
    content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, technique, when_to_apply, tags, project)
    VALUES (new.id, new.title, new.technique, new.when_to_apply, new.tags, new.project);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, technique, when_to_apply, tags, project)
    VALUES ('delete', old.id, old.title, old.technique, old.when_to_apply, old.tags, old.project);
END;
"""

POSTGRES_SCHEMA = [
    f"CREATE TABLE IF NOT EXISTS boards ({_BOARD_COLS.format(real='DOUBLE PRECISION')})",
    """CREATE TABLE IF NOT EXISTS messages (
        id BIGSERIAL PRIMARY KEY, board_id TEXT NOT NULL REFERENCES boards(id), author TEXT NOT NULL,
        kind TEXT NOT NULL, content TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, reply_to BIGINT)""",
    "CREATE INDEX IF NOT EXISTS messages_board ON messages(board_id, id)",
    "CREATE INDEX IF NOT EXISTS messages_reply ON messages(reply_to)",
    """CREATE TABLE IF NOT EXISTS cursors (
        board_id TEXT NOT NULL, reader TEXT NOT NULL, last_id BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (board_id, reader))""",
    """CREATE TABLE IF NOT EXISTS presence (
        board_id TEXT NOT NULL, agent TEXT NOT NULL, seen_at DOUBLE PRECISION NOT NULL,
        state TEXT NOT NULL DEFAULT 'active', cwd TEXT, toplevel TEXT, branch TEXT, nickname TEXT, model TEXT,
        waiting TEXT, waiting_at DOUBLE PRECISION, level INTEGER,
        PRIMARY KEY (board_id, agent))""",
    "ALTER TABLE presence ADD COLUMN IF NOT EXISTS waiting TEXT",
    "ALTER TABLE presence ADD COLUMN IF NOT EXISTS level INTEGER",
    "ALTER TABLE presence ADD COLUMN IF NOT EXISTS waiting_at DOUBLE PRECISION",
    "ALTER TABLE presence ADD COLUMN IF NOT EXISTS nickname TEXT",
    "ALTER TABLE presence ADD COLUMN IF NOT EXISTS model TEXT",
    """CREATE TABLE IF NOT EXISTS claims (
        board_id TEXT NOT NULL, toplevel TEXT NOT NULL DEFAULT '', path TEXT NOT NULL, agent TEXT NOT NULL,
        since DOUBLE PRECISION NOT NULL, locked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (board_id, toplevel, path))""",
    "ALTER TABLE claims ADD COLUMN IF NOT EXISTS locked INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE IF NOT EXISTS identities (
        client TEXT PRIMARY KEY, agent TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS wake_tickets (
        token TEXT PRIMARY KEY, board_id TEXT NOT NULL, agent TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS milestones (
        id BIGSERIAL PRIMARY KEY, board_id TEXT NOT NULL REFERENCES boards(id), text TEXT NOT NULL,
        position DOUBLE PRECISION NOT NULL, created_by TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        done_by TEXT, done_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS memories (
        id BIGSERIAL PRIMARY KEY, board_id TEXT NOT NULL REFERENCES boards(id), project TEXT NOT NULL,
        title TEXT NOT NULL, technique TEXT NOT NULL, when_to_apply TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL,
        search tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('english', title), 'A') || setweight(to_tsvector('english', tags), 'A') ||
            setweight(to_tsvector('english', when_to_apply), 'B') ||
            setweight(to_tsvector('english', technique), 'B') ||
            setweight(to_tsvector('english', project), 'C')) STORED)""",
    "CREATE INDEX IF NOT EXISTS memories_search ON memories USING GIN (search)",
]


class BoardError(Exception):
    """A user-facing error (bad key, closed board, ...)."""


# ---- backends ---------------------------------------------------------------


class _Sqlite:
    kind = "sqlite"

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Switching to WAL can return SQLITE_BUSY without consulting the busy handler when
        # several sessions start at once, so retry it (and the schema setup) briefly.
        for attempt in range(50):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SQLITE_SCHEMA)
                self._migrate()
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.1)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, col, ddl in (
            ("boards", "summary", "summary TEXT"),
            ("boards", "summary_through", "summary_through INTEGER NOT NULL DEFAULT 0"),
            ("boards", "compact_lock", "compact_lock REAL"),
            ("boards", "compact_error", "compact_error TEXT"),
            ("messages", "reply_to", "reply_to INTEGER"),
            ("claims", "locked", "locked INTEGER NOT NULL DEFAULT 0"),
            ("presence", "nickname", "nickname TEXT"),
            ("presence", "model", "model TEXT"),
            ("presence", "waiting", "waiting TEXT"),
            ("presence", "waiting_at", "waiting_at REAL"),
            ("presence", "level", "level INTEGER"),
        ):
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                try:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                except sqlite3.OperationalError as e:  # another process added it first
                    if "duplicate column" not in str(e):
                        raise
        self._rebuild_claims_if_stale()

    def _rebuild_claims_if_stale(self) -> None:
        """A pre-release build created claims with PRIMARY KEY (board_id, path); claims are now per
        checkout. Rebuild that table in place, keeping its rows (they're short-lived anyway)."""
        def pk():
            rows = sorted((r for r in self.conn.execute("PRAGMA table_info(claims)") if r["pk"]), key=lambda r: r["pk"])
            return [r["name"] for r in rows]

        if pk() in ([], ["board_id", "toplevel", "path"]):
            return
        with self.tx():
            if pk() == ["board_id", "toplevel", "path"]:
                return  # another process rebuilt it while we waited for the lock
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(claims)")}
            locked = "COALESCE(locked, 0)" if "locked" in cols else "0"
            self.conn.execute("ALTER TABLE claims RENAME TO claims_stale")
            self.conn.execute(
                "CREATE TABLE claims (board_id TEXT NOT NULL, toplevel TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,"
                " agent TEXT NOT NULL, since REAL NOT NULL, locked INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (board_id, toplevel, path))"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO claims (board_id, toplevel, path, agent, since, locked)"
                f" SELECT board_id, COALESCE(toplevel, ''), path, agent, since, {locked} FROM claims_stale"
            )
            self.conn.execute("DROP TABLE claims_stale")

    def execute(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise


class _Postgres:
    kind = "postgres"

    def __init__(self, url: str):
        import psycopg

        self._psycopg = psycopg
        self.url = url
        self._connect()
        # Several sessions may start at once; serialize schema creation with an advisory lock.
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(7362516)")
            for ddl in POSTGRES_SCHEMA:
                self.conn.execute(ddl)

    def _connect(self) -> None:
        from psycopg.rows import dict_row

        # prepare_threshold=None: no server-side prepared statements, so transaction-mode poolers
        # (Supabase's pooler on port 6543) work too.
        self.conn = self._psycopg.connect(
            self.url, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=10
        )

    def execute(self, sql: str, args=()):
        sql = sql.replace("?", "%s")
        try:
            return self.conn.execute(sql, args)
        except self._psycopg.OperationalError:
            # Long-lived MCP servers outlive idle connections (pooler/server timeouts): reconnect once.
            if self.conn.closed or self.conn.broken:
                self._connect()
                return self.conn.execute(sql, args)
            raise

    @contextmanager
    def tx(self):
        with self.conn.transaction():
            yield


# ---- records ----------------------------------------------------------------


@dataclass
class Board:
    id: str
    name: str
    project: str
    goal: str
    created_by: str
    created_at: float
    status: str
    closed_by: str | None
    closed_at: float | None
    outcome: str | None
    distill_status: str | None
    distill_error: str | None
    summary: str | None = None
    summary_through: int = 0
    compact_lock: float | None = None
    compact_error: str | None = None

    def to_dict(self) -> dict:
        """Metadata for clients; the summary itself is returned separately by join."""
        hidden = {"summary", "compact_lock"}
        return {k: v for k, v in self.__dict__.items() if k not in hidden}


@dataclass
class Message:
    id: int
    board_id: str
    author: str
    kind: str
    content: str
    created_at: float
    reply_to: int | None = None


def fmt_message(m: Message, context: str = "", name: str | None = None) -> str:
    """One line per message; `note` is the unmarked default kind. name: the author's nickname."""
    kind = "" if m.kind == "note" else "/" + m.kind
    reply = f" re #{m.reply_to}" + (f" ({context})" if context else "") if m.reply_to else ""
    return f"#{m.id} {name or m.author}{kind}{reply}: {m.content}"


def fmt_messages(msgs: list[Message]) -> str:
    return "\n".join(fmt_message(m) for m in msgs)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


_BOARD_FIELDS = set(Board.__dataclass_fields__)
_MESSAGE_FIELDS = set(Message.__dataclass_fields__)


def _board(row) -> Board:
    return Board(**{k: row[k] for k in row.keys() if k in _BOARD_FIELDS})


def _message(row) -> Message:
    return Message(**{k: row[k] for k in row.keys() if k in _MESSAGE_FIELDS})


# ---- store ------------------------------------------------------------------


class Store:
    def __init__(self, url: Path | str | None = None):
        self.url = str(url) if url else database_url()
        self.path = self.url  # older name; threads pass this to open their own connection
        self.db = _Postgres(self.url) if is_postgres(self.url) else _Sqlite(self.url)

    def _one(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchone()

    def _all(self, sql: str, args=()) -> list:
        return list(self.db.execute(sql, args))

    # ---- boards -----------------------------------------------------------

    def create_board(self, name: str, project: str, created_by: str, goal: str = "") -> tuple[Board, str]:
        """Create a board. Returns (board, key). The key is only shown once; we store its hash."""
        board_id = "b_" + secrets.token_hex(4)
        key = f"brd_{secrets.token_urlsafe(24)}"
        self.db.execute(
            "INSERT INTO boards (id, key_hash, name, project, goal, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (board_id, hash_key(key), name.strip(), project.strip(), goal.strip(), created_by, time.time()),
        )
        return self.get_board(board_id), key

    def get_board(self, board_id: str) -> Board:
        row = self._one("SELECT * FROM boards WHERE id = ?", (board_id,))
        if not row:
            raise BoardError(f"no board {board_id}")
        return _board(row)

    def board_for_key(self, key: str) -> Board:
        row = self._one("SELECT * FROM boards WHERE key_hash = ?", (hash_key(key),))
        if not row:
            raise BoardError("invalid board key")
        return _board(row)

    def list_boards(self, project: str | None = None, status: str | None = "open") -> list[Board]:
        """Board metadata only (never keys) — lets an agent see that a board exists and ask for its key."""
        sql, args = "SELECT * FROM boards WHERE 1=1", []
        if project:
            sql += " AND project = ?"
            args.append(project)
        if status:
            sql += " AND status = ?"
            args.append(status)
        return [_board(r) for r in self._all(sql + " ORDER BY created_at DESC", args)]

    def close_board(self, key: str, closed_by: str, outcome: str = "") -> Board:
        return self.close_board_by_id(self.board_for_key(key).id, closed_by, outcome)

    def close_board_by_id(self, board_id: str, closed_by: str, outcome: str = "") -> Board:
        """Close a board you already resolved (the viewer knows ids, not keys)."""
        board = self.get_board(board_id)
        if board.status == "closed":
            raise BoardError(f"board {board.id} is already closed")
        self.db.execute(
            "UPDATE boards SET status='closed', closed_by=?, closed_at=?, outcome=?, distill_status='pending'"
            " WHERE id=? AND status='open'",
            (closed_by, time.time(), outcome.strip(), board.id),
        )
        self.db.execute("DELETE FROM claims WHERE board_id=?", (board.id,))
        self.db.execute("UPDATE presence SET waiting=NULL, waiting_at=NULL WHERE board_id=?", (board.id,))
        return self.get_board(board.id)

    def reopen_board(self, key: str, reopened_by: str) -> Board:
        """Spin a closed board back up. Its history is kept; closing again re-distills it."""
        board = self.board_for_key(key)
        if board.status == "open":
            raise BoardError(f"board {board.id} is already open")
        self.db.execute(
            "UPDATE boards SET status='open', closed_by=NULL, closed_at=NULL, distill_status=NULL, distill_error=NULL"
            " WHERE id=?",
            (board.id,),
        )
        self.db.execute(
            "INSERT INTO messages (board_id, author, kind, content, created_at) VALUES (?, ?, 'note', ?, ?)",
            (board.id, reopened_by, f"[board reopened by {reopened_by}]", time.time()),
        )
        return self.get_board(board.id)

    def stale_pending_boards(self, closed_before: float) -> list[str]:
        rows = self._all(
            "SELECT id FROM boards WHERE status='closed' AND distill_status='pending' AND closed_at < ?",
            (closed_before,),
        )
        return [r["id"] for r in rows]

    # ---- compaction -------------------------------------------------------

    def claim_compaction(self, board_id: str, stale_after: float = 300) -> bool:
        """Only one process compacts a board at a time; a stuck claim expires after stale_after seconds."""
        now = time.time()
        cur = self.db.execute(
            "UPDATE boards SET compact_lock=? WHERE id=? AND (compact_lock IS NULL OR compact_lock < ?)",
            (now, board_id, now - stale_after),
        )
        return cur.rowcount == 1

    def save_summary(self, board_id: str, summary: str, through_id: int) -> None:
        # Never move the cutoff backwards if a slower compaction finishes after a newer one.
        self.db.execute(
            "UPDATE boards SET summary=?, summary_through=?, compact_lock=NULL, compact_error=NULL"
            " WHERE id=? AND summary_through <= ?",
            (summary, through_id, board_id, through_id),
        )
        self.db.execute("UPDATE boards SET compact_lock=NULL WHERE id=?", (board_id,))

    def release_compaction(self, board_id: str, error: str | None = None) -> None:
        """On error the lock is left in place as a cooldown (it expires like a stale claim),
        so a failing compaction isn't retried on every join."""
        lock = time.time() if error else None
        self.db.execute("UPDATE boards SET compact_lock=?, compact_error=? WHERE id=?", (lock, error, board_id))

    def messages_between(self, board_id: str, after_id: int, through_id: int | None = None) -> list[Message]:
        sql, args = "SELECT * FROM messages WHERE board_id=? AND id>?", [board_id, after_id]
        if through_id is not None:
            sql += " AND id<=?"
            args.append(through_id)
        return [_message(r) for r in self._all(sql + " ORDER BY id", args)]

    def set_distill_status(self, board_id: str, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE boards SET distill_status=?, distill_error=? WHERE id=?", (status, error, board_id))

    # ---- messages ---------------------------------------------------------

    def post(self, key: str, author: str, content: str, kind: str = "note", reply_to: int | None = None) -> Message:
        return self.post_to(self.board_for_key(key), author, content, kind, reply_to)

    def post_to(self, board: Board | str, author: str, content: str, kind: str = "note",
                reply_to: int | None = None) -> Message:
        """Post to a board you already resolved (hooks know the board id, not the key)."""
        if isinstance(board, str):
            board = self.get_board(board)
        if board.status != "open":
            raise BoardError(f"board {board.id} is closed; it is read-only")
        kind = KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
        if kind not in MESSAGE_KINDS:
            kind = "note"
        if not content.strip():
            raise BoardError("empty message")
        from .tidy import tidy

        content = tidy(content)  # "474 pass, ruff+mypy clean" -> "tests pass"
        content, conflicts = self._quiet_files(board, author, kind, content)
        if len(content) > MAX_POST_CHARS:
            raise BoardError(
                f"{len(content)} chars, max {MAX_POST_CHARS}. Every agent rereads posts: "
                "write caveman, point to files instead of pasting."
            )
        if reply_to:
            row = self._one("SELECT board_id FROM messages WHERE id=?", (reply_to,))
            if not row or row["board_id"] != board.id:
                raise BoardError(f"reply_to #{reply_to} is not on this board")
        row = self._one(
            "INSERT INTO messages (board_id, author, kind, content, created_at, reply_to)"
            " VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (board.id, author, kind, content.strip(), time.time(), reply_to or None),
        )
        m = self.get_message(row["id"])
        m.conflicts = conflicts  # files named in a status that someone else holds
        return m

    def _quiet_files(self, board: Board, author: str, kind: str, content: str) -> tuple[str, list]:
        """Keep file names out of posts; the inventory already shows who works on what.
        - any post: 'changed: a.py, b.py' lists are dropped
        - status: files it says it owns ("own src/parser/*") are claimed into the inventory
        - status and note: the author's own claimed files are dropped
        Files someone else holds stay in the text, since there the name is information."""
        from .claims import normalize, overlap, owned_paths, strip_file_lists, strip_paths

        text = strip_file_lists(content)
        conflicts: list = []
        if kind in ("status", "note"):
            top = (self.presence(board.id).get(author) or {}).get("toplevel")
            if kind == "status":
                named = list(dict.fromkeys(normalize(p, top) for p in owned_paths(text)))
                if named:
                    conflicts = self.claim(board.id, author, named, top)
            own = [c["path"] for c in self.claims(board.id) if c["agent"] == author]
            if own:
                text = strip_paths(text, lambda p: any(overlap(normalize(p, top), o) for o in own))
        if text.strip() != content.strip() and len(text.strip(" .,;:")) < 3:
            text = "working"  # the whole post was file names (a short post like "42" or "ok" stays)
        return text, conflicts

    def get_message(self, message_id: int) -> Message:
        return _message(self._one("SELECT * FROM messages WHERE id=?", (message_id,)))

    def max_message_id(self, board_id: str) -> int:
        return self._one("SELECT COALESCE(MAX(id), 0) AS n FROM messages WHERE board_id=?", (board_id,))["n"]

    def current_statuses(self, board_id: str) -> list[Message]:
        """Newest status per agent; older statuses are superseded."""
        rows = self._all(
            "SELECT * FROM messages WHERE id IN (SELECT MAX(id) FROM messages"
            " WHERE board_id=? AND kind='status' GROUP BY author) ORDER BY id",
            (board_id,),
        )
        return [_message(r) for r in rows]

    def open_items(self, board_id: str) -> list[Message]:
        """Questions and blockers nobody has replied to yet."""
        rows = self._all(
            "SELECT * FROM messages m WHERE board_id=? AND kind IN ('question','blocker','promise')"
            " AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.reply_to = m.id) ORDER BY id",
            (board_id,),
        )
        return [_message(r) for r in rows]

    def log_after(self, board_id: str, after_id: int) -> list[Message]:
        """Durable entries, answered questions/blockers and notes after a cutoff (statuses and open
        items are shown live from the whole board instead)."""
        rows = self._all(
            "SELECT * FROM messages m WHERE board_id=? AND id>? AND (kind IN ('decision','result','deadend','answer','note')"
            " OR (kind IN ('question','blocker','promise') AND EXISTS (SELECT 1 FROM messages r WHERE r.reply_to = m.id)))"
            " ORDER BY id",
            (board_id, after_id),
        )
        return [_message(r) for r in rows]

    def snippets(self, ids: set[int], width: int = 70) -> dict[int, str]:
        """Short excerpts of replied-to messages, so a reply reads on its own."""
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        rows = self._all(f"SELECT id, content FROM messages WHERE id IN ({q})", list(ids))
        return {r["id"]: (r["content"][:width] + "…" if len(r["content"]) > width else r["content"]) for r in rows}

    def read(self, key: str, since_id: int = 0, limit: int = 200) -> tuple[Board, list[Message]]:
        board = self.board_for_key(key)
        rows = self._all(
            "SELECT * FROM messages WHERE board_id=? AND id>? ORDER BY id LIMIT ?", (board.id, since_id, limit)
        )
        return board, [_message(r) for r in rows]

    def all_messages(self, board_id: str) -> list[Message]:
        return [_message(r) for r in self._all("SELECT * FROM messages WHERE board_id=? ORDER BY id", (board_id,))]

    def wait_for_messages(self, key: str, since_id: int, timeout: float, poll: float = 1.0) -> tuple[Board, list[Message]]:
        """Long-poll: return as soon as anything newer than since_id exists, or after timeout."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            board, msgs = self.read(key, since_id)
            if msgs or board.status != "open" or time.monotonic() >= deadline:
                return board, msgs
            time.sleep(poll)

    # ---- read cursors (shared by a session's MCP server and its hooks) ---

    def get_cursor(self, board_id: str, reader: str) -> int | None:
        row = self._one("SELECT last_id FROM cursors WHERE board_id=? AND reader=?", (board_id, reader))
        return row["last_id"] if row else None

    def set_cursor(self, board_id: str, reader: str, last_id: int, rewind: bool = False) -> None:
        """Advance a reader's cursor (never backwards unless rewind=True)."""
        update = "excluded.last_id" if rewind else \
            "CASE WHEN excluded.last_id > cursors.last_id THEN excluded.last_id ELSE cursors.last_id END"
        self.db.execute(
            "INSERT INTO cursors (board_id, reader, last_id) VALUES (?, ?, ?)"
            f" ON CONFLICT (board_id, reader) DO UPDATE SET last_id = {update}",
            (board_id, reader, last_id),
        )

    def unread(self, board_id: str, reader: str, limit: int = 500) -> list[Message]:
        """Others' messages after the reader's cursor (a reader's own posts are never unread)."""
        cursor = self.get_cursor(board_id, reader) or 0
        rows = self._all(
            "SELECT * FROM messages WHERE board_id=? AND id>? AND author<>? ORDER BY id LIMIT ?",
            (board_id, cursor, reader, limit),
        )
        return [_message(r) for r in rows]

    # ---- presence (who is around, and where they work) --------------------

    def touch(self, board_id: str, agent: str, cwd: str | None = None, toplevel: str | None = None,
              branch: str | None = None, level: int | None = None) -> None:
        """Record activity. The MCP server doesn't know the agent's working folder, so missing
        fields keep their last known value (hooks fill them in); the server records its SERVER_LEVEL."""
        self.db.execute(
            "INSERT INTO presence (board_id, agent, seen_at, state, cwd, toplevel, branch, level)"
            " VALUES (?, ?, ?, 'active', ?, ?, ?, ?) ON CONFLICT (board_id, agent) DO UPDATE SET"
            " seen_at = excluded.seen_at, state = 'active',"
            " cwd = COALESCE(excluded.cwd, presence.cwd),"
            " toplevel = COALESCE(excluded.toplevel, presence.toplevel),"
            " branch = COALESCE(excluded.branch, presence.branch),"
            " level = COALESCE(excluded.level, presence.level)",
            (board_id, agent, time.time(), cwd, toplevel, branch, level),
        )

    def mark_left(self, board_id: str, agent: str) -> None:
        self.db.execute(
            "INSERT INTO presence (board_id, agent, seen_at, state) VALUES (?, ?, ?, 'left')"
            " ON CONFLICT (board_id, agent) DO UPDATE SET state = 'left', seen_at = excluded.seen_at",
            (board_id, agent, time.time()),
        )

    def end_session(self, board_id: str, agent: str, by: str = "user", reason: str | None = None,
                    announce: bool = True) -> Message | None:
        """The one way a session leaves a board, whoever notices it's over (the agent's SessionEnd hook,
        its parent session for a finished subagent, the user, the stale-session sweep): mark it left,
        free its files, unpin it, and tell the board. Unkept promises are always flagged; announce=False
        skips the plain "its files are free" note. If it's still alive, it reappears when it next acts."""
        board = self.get_board(board_id)
        name = self.labels(board_id).get(agent) or short_id(agent)
        self.mark_left(board_id, agent)
        self.release(board_id, agent, ["*"])
        self.set_waiting(board_id, agent, None)
        if board.status != "open":
            return None
        promises = [m for m in self.open_items(board_id) if m.kind == "promise" and m.author == agent]
        why = reason or f"was ended by the {by}"
        if promises:
            ids = ", ".join(f"#{m.id} ({_snip(m.content, 50)})" for m in promises[:3])
            return self.post_to(board, by, f"{name}'s session {why}. Unkept promise: {ids}. Pick it up or re-plan.",
                                "blocker")
        if not announce:
            return None
        return self.post_to(board, by, f"{name}'s session {why}; its files are free.", "note")

    def end_stale_sessions(self, older_than: float | None = None) -> list[tuple[str, str]]:
        """End sessions with no activity for SESSION_TIMEOUT (3 days by default) on open boards."""
        older_than = SESSION_TIMEOUT if older_than is None else older_than
        rows = self._all(
            "SELECT p.board_id, p.agent FROM presence p JOIN boards b ON b.id = p.board_id"
            " WHERE b.status='open' AND p.state='active' AND p.seen_at < ?",
            (time.time() - older_than,),
        )
        days = round(older_than / 86400, 1)
        for r in rows:
            self.end_session(r["board_id"], r["agent"], by="huddlehut",
                             reason=f"timed out after {days:g} days without activity")
        return [(r["board_id"], r["agent"]) for r in rows]

    def presence(self, board_id: str) -> dict[str, dict]:
        return {r["agent"]: dict(r) for r in self._all("SELECT * FROM presence WHERE board_id=?", (board_id,))}

    def live_agents(self, board_id: str, exclude: str | None = None) -> dict[str, dict]:
        """Agents active within IDLE_AFTER seconds that haven't left."""
        cutoff = time.time() - IDLE_AFTER
        return {a: p for a, p in self.presence(board_id).items()
                if p["state"] == "active" and p["seen_at"] >= cutoff and a != exclude}

    # ---- identity -----------------------------------------------------------
    # A client session (e.g. one Claude Code conversation) keeps one agent id for good, so a restarted or
    # resumed session is the same agent: same nickname, read position, files and promises.

    def identity(self, client: str, fresh: str) -> str:
        """The agent id this client session had before, or `fresh` (remembered from now on)."""
        self.db.execute("INSERT INTO identities (client, agent, created_at) VALUES (?, ?, ?)"
                        " ON CONFLICT (client) DO NOTHING", (client, fresh, time.time()))
        return self._one("SELECT agent FROM identities WHERE client=?", (client,))["agent"]

    def remember_identity(self, client: str, agent: str) -> bool:
        """Make `agent` this client session's identity, unless another session already is that agent: an
        identity belongs to one conversation (two Codex threads must never become one agent)."""
        if self.owner_of(agent) not in (None, client):
            return False
        self.db.execute("INSERT INTO identities (client, agent, created_at) VALUES (?, ?, ?)"
                        " ON CONFLICT (client) DO UPDATE SET agent = excluded.agent", (client, agent, time.time()))
        return True

    def owner_of(self, agent: str) -> str | None:
        """The client session that is this agent, if one is on record."""
        row = self._one("SELECT client FROM identities WHERE agent = ? ORDER BY created_at DESC LIMIT 1", (agent,))
        return row["client"] if row else None

    def issue_ticket(self, board_id: str, agent: str) -> str:
        """A one-time ticket that lets whoever presents it on this board become this agent. The wake
        service hands one to an idle Codex agent with its wake message: if Codex resumes the thread with a
        fresh board server, join_board(ticket=...) gets the agent its identity back."""
        now = time.time()
        self.db.execute("DELETE FROM wake_tickets WHERE created_at < ?", (now - TICKET_TTL,))
        token = f"wk_{secrets.token_urlsafe(9)}"
        self.db.execute("INSERT INTO wake_tickets (token, board_id, agent, created_at) VALUES (?, ?, ?, ?)",
                        (token, board_id, agent, now))
        return token

    def redeem_ticket(self, token: str, board_id: str) -> str | None:
        """The agent a ticket stands for, if it's for this board and still fresh. Each ticket works once."""
        row = self._one("SELECT agent FROM wake_tickets WHERE token = ? AND board_id = ? AND created_at >= ?",
                        (token, board_id, time.time() - TICKET_TTL))
        self.db.execute("DELETE FROM wake_tickets WHERE token = ?", (token,))
        return row["agent"] if row else None

    def dormant_holder(self, board_id: str, nickname: str, tool: str, client: str | None = None) -> str | None:
        """The agent of the same tool that held this nickname here and has since gone idle or left: most
        likely the same agent coming back after a restart. An active holder is someone else, and so is
        one that belongs to another client session."""
        cutoff = time.time() - IDLE_AFTER
        for agent, p in self.presence(board_id).items():
            if (p.get("nickname") == nickname.strip().lower() and tool_of(agent) == tool
                    and (p["state"] == "left" or p["seen_at"] < cutoff)
                    and self.owner_of(agent) in (None, client)):
                return agent
        return None

    # ---- nicknames ----------------------------------------------------------
    # Agents pick a cute one-word name when they join. The id (claude-code-3f2a) stays the key
    # for cursors, claims and hooks; the nickname is what everyone reads and @mentions.

    def nicknames(self, board_id: str) -> dict[str, str]:
        return {r["agent"]: r["nickname"] for r in self._all(
            "SELECT agent, nickname FROM presence WHERE board_id=? AND nickname IS NOT NULL", (board_id,))}

    def set_nickname(self, board_id: str, agent: str, wanted: str | None = None) -> tuple[str, str | None]:
        """Give an agent a nickname on a board. Returns (nickname, why the wanted one wasn't used)."""
        current = self.nicknames(board_id)
        taken = {n for a, n in current.items() if a != agent} | {self.user_name()}
        why = None
        nick = re.sub(r"[^a-z0-9-]", "", (wanted or "").strip().lower().replace(" ", "-"))[:16].strip("-")
        if wanted is not None and wanted.strip():
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,15}", nick) or nick in RESERVED_NICKNAMES:
                why, nick = f"'{wanted.strip()}' isn't usable (one short word, letters first)", ""
            elif nick in taken:
                why, nick = f"'{nick}' is taken", ""
        if not nick:
            nick = current.get(agent) if current.get(agent) and current[agent] not in taken else ""
        if not nick:
            nick = default_nickname(agent, taken)
        self.touch(board_id, agent)
        self.db.execute("UPDATE presence SET nickname=? WHERE board_id=? AND agent=?", (nick, board_id, agent))
        return nick, why

    def set_waiting(self, board_id: str, agent: str, question: str | None) -> None:
        """Record that an agent's session is waiting on the user (its last reply, trimmed), or clear it."""
        if question:
            self.touch(board_id, agent)
            self.db.execute("UPDATE presence SET waiting=?, waiting_at=? WHERE board_id=? AND agent=?",
                            (ask_tail(question), time.time(), board_id, agent))
        else:
            self.db.execute("UPDATE presence SET waiting=NULL, waiting_at=NULL WHERE board_id=? AND agent=?"
                            " AND waiting IS NOT NULL", (board_id, agent))

    def set_model(self, board_id: str, agent: str, model: str) -> None:
        model = re.sub(r"[^\w.\-/: ]", "", model.strip())[:48]
        if model:
            self.touch(board_id, agent)
            self.db.execute("UPDATE presence SET model=? WHERE board_id=? AND agent=?", (model, board_id, agent))

    def labels(self, board_id: str) -> dict[str, str]:
        """Display name per agent: its nickname, else its 4-char tag. The user shows by their name."""
        names = self.nicknames(board_id)
        authors = {r["author"] for r in self._all("SELECT DISTINCT author FROM messages WHERE board_id=?", (board_id,))}
        holders = {c["agent"] for c in self.claims(board_id)}
        everyone = {*names, *self.presence(board_id), *authors, *holders}
        if USER in everyone:
            names[USER] = self.user_name()
        return {a: names.get(a) or short_id(a) for a in everyone}

    # ---- the user's name ----------------------------------------------------
    # The human's posts are always authored "user" (that's what @user, pins and agents key on); this is
    # the name everyone sees for it. On a shared database everyone's viewer posts as "user" and shares it.

    def user_name(self) -> str:
        row = self._one("SELECT value FROM settings WHERE key='user_name'")
        return row["value"] if row else USER

    def set_user_name(self, wanted: str) -> str:
        name = re.sub(r"[^a-z0-9-]", "", wanted.strip().lower().replace(" ", "-"))[:16].strip("-")
        if not name or name == USER:
            self.db.execute("DELETE FROM settings WHERE key='user_name'")
            return USER
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,15}", name) or name in RESERVED_NICKNAMES:
            raise BoardError(f"'{wanted.strip()}' isn't usable: one short word, letters first")
        clash = self._one("SELECT b.name FROM presence p JOIN boards b ON b.id = p.board_id"
                          " WHERE b.status='open' AND p.nickname=? AND p.state != 'left' LIMIT 1", (name,))
        if clash:
            raise BoardError(f"an agent on {clash['name']} is called {name}; pick another name")
        self.db.execute("INSERT INTO settings (key, value) VALUES ('user_name', ?)"
                        " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (name,))
        return name

    # ---- file inventory (who is working on which files) --------------------

    def claims(self, board_id: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM claims WHERE board_id=? ORDER BY agent, path", (board_id,))]

    def conflicts(self, board_id: str, agent: str, paths, toplevel: str | None = None) -> list[tuple[str, dict]]:
        """(path, other agent's claim) for paths overlapping a claim held by another active agent in
        the same checkout. Idle or departed agents' claims, and other worktrees, don't count.
        A claim made without a known checkout ('') counts everywhere."""
        from .claims import _overlap

        live = self.live_agents(board_id, exclude=agent)
        out = []
        for c in self.claims(board_id):
            if c["agent"] == agent or c["agent"] not in live:
                continue
            if toplevel and c["toplevel"] and c["toplevel"] != toplevel:
                continue
            out += [(p, c) for p in paths if _overlap(p, c["path"])]
        return out

    def claim(self, board_id: str, agent: str, paths, toplevel: str | None = None,
              lock: bool = False) -> list[tuple[str, dict]]:
        """Claim paths (files or globs) in a checkout. Paths another active agent holds there are not
        taken; those are returned as conflicts. Claims of idle or departed agents are taken over.
        lock=True also blocks other agents' edits of these paths until unlocked or released."""
        conflicts = self.conflicts(board_id, agent, paths, toplevel)
        held = {p for p, _ in conflicts}
        now = time.time()
        for p in paths:
            if p not in held:
                self.db.execute(
                    "INSERT INTO claims (board_id, toplevel, path, agent, since, locked) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (board_id, toplevel, path) DO UPDATE SET"
                    " locked = CASE WHEN excluded.locked = 1 THEN 1"
                    " WHEN claims.agent = excluded.agent THEN claims.locked ELSE 0 END,"
                    " agent = excluded.agent, since = excluded.since",
                    (board_id, toplevel or "", p, agent, now, 1 if lock else 0),
                )
        return conflicts

    def unlock(self, board_id: str, agent: str, paths=("*",)) -> int:
        """Drop the lock but keep the claim; "*" unlocks all of this agent's files."""
        if "*" in paths:
            return self.db.execute("UPDATE claims SET locked=0 WHERE board_id=? AND agent=?", (board_id, agent)).rowcount
        q = ",".join("?" * len(paths))
        return self.db.execute(
            f"UPDATE claims SET locked=0 WHERE board_id=? AND agent=? AND path IN ({q})", (board_id, agent, *paths)
        ).rowcount

    def release(self, board_id: str, agent: str, paths=("*",)) -> int:
        """Release this agent's claims; "*" releases all of them."""
        if "*" in paths:
            return self.db.execute("DELETE FROM claims WHERE board_id=? AND agent=?", (board_id, agent)).rowcount
        q = ",".join("?" * len(paths))
        return self.db.execute(
            f"DELETE FROM claims WHERE board_id=? AND agent=? AND path IN ({q})", (board_id, agent, *paths)
        ).rowcount

    # ---- collective goal ---------------------------------------------------
    # The board's goal plus a checklist of milestones everyone works toward. The user sets them in the
    # viewer; agents check milestones off with the goal tool.

    def milestones(self, board_id: str) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT * FROM milestones WHERE board_id=? ORDER BY position, id", (board_id,))]

    def add_milestone(self, board_id: str, text: str, by: str) -> dict:
        text = " ".join(text.split())[:300]
        if not text:
            raise BoardError("empty milestone")
        pos = (self._one("SELECT COALESCE(MAX(position), 0) AS p FROM milestones WHERE board_id=?", (board_id,))["p"]
               or 0) + 1
        row = self._one("INSERT INTO milestones (board_id, text, position, created_by, created_at)"
                        " VALUES (?, ?, ?, ?, ?) RETURNING id", (board_id, text, pos, by, time.time()))
        return self._milestone(board_id, row["id"])

    def _milestone(self, board_id: str, mid: int) -> dict:
        row = self._one("SELECT * FROM milestones WHERE id=? AND board_id=?", (mid, board_id))
        if not row:
            raise BoardError(f"no milestone {mid} on this board")
        return dict(row)

    def set_milestone_done(self, board_id: str, mid: int, by: str, done: bool = True) -> dict:
        self._milestone(board_id, mid)
        if done:
            self.db.execute("UPDATE milestones SET done_by=?, done_at=? WHERE id=? AND done_at IS NULL",
                            (by, time.time(), mid))
        else:
            self.db.execute("UPDATE milestones SET done_by=NULL, done_at=NULL WHERE id=?", (mid,))
        return self._milestone(board_id, mid)

    def delete_milestone(self, board_id: str, mid: int) -> None:
        self._milestone(board_id, mid)
        self.db.execute("DELETE FROM milestones WHERE id=?", (mid,))

    def set_goal(self, board_id: str, goal: str) -> Board:
        self.db.execute("UPDATE boards SET goal=? WHERE id=?", (" ".join(goal.split())[:1000], board_id))
        return self.get_board(board_id)

    # ---- viewer -------------------------------------------------------------

    def board_stats(self) -> dict[str, dict]:
        """Per board: message count and last activity, for the viewer's board list."""
        rows = self._all("SELECT board_id, COUNT(*) AS n, MAX(created_at) AS last_at FROM messages GROUP BY board_id")
        return {r["board_id"]: {"messages": r["n"], "last_at": r["last_at"]} for r in rows}

    def list_memories(self, project: str | None = None, board_id: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM memories WHERE 1=1", []
        if project:
            sql += " AND project = ?"
            args.append(project)
        if board_id:
            sql += " AND board_id = ?"
            args.append(board_id)
        rows = self._all(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", [*args, limit])
        return [{"id": r["id"], "title": r["title"], "technique": r["technique"], "when_to_apply": r["when_to_apply"],
                 "tags": r["tags"].split(), "project": r["project"], "from_board": r["board_id"]} for r in rows]

    # ---- memories ---------------------------------------------------------

    def add_memories(self, board_id: str, project: str, lessons: list[dict]) -> int:
        now = time.time()
        with self.db.tx():
            self.db.execute("DELETE FROM memories WHERE board_id=?", (board_id,))  # idempotent re-distill
            for l in lessons:
                self.db.execute(
                    "INSERT INTO memories (board_id, project, title, technique, when_to_apply, tags, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (board_id, project, l["title"], l["technique"], l["when_to_apply"], " ".join(l.get("tags", [])), now),
                )
        return len(lessons)

    def search_memories(self, query: str, project: str | None = None, limit: int = 8) -> list[dict]:
        """Full-text retrieval over distilled lessons (BM25 on SQLite, ts_rank on Postgres).
        Words are OR'ed so partial matches still rank."""
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
        if not terms:
            return []
        args: list = []
        if self.db.kind == "postgres":
            sql = (
                "SELECT m.*, ts_rank(m.search, q) AS score FROM memories m, to_tsquery('english', ?) q"
                " WHERE m.search @@ q"
            )
            args.append(" | ".join(terms))
            order = "score DESC"
        else:
            sql = (
                "SELECT m.*, bm25(memories_fts, 4.0, 2.0, 2.0, 3.0, 1.0) AS score FROM memories_fts"
                " JOIN memories m ON m.id = memories_fts.rowid WHERE memories_fts MATCH ?"
            )
            args.append(" OR ".join(f'"{t}"' for t in terms))
            order = "score"
        if project:
            sql += " AND m.project = ?"
            args.append(project)
        sql += f" ORDER BY {order} LIMIT ?"
        args.append(limit)
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "technique": r["technique"],
                "when_to_apply": r["when_to_apply"],
                "tags": r["tags"].split(),
                "project": r["project"],
                "from_board": r["board_id"],
            }
            for r in self._all(sql, args)
        ]
