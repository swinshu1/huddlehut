from types import SimpleNamespace

import pytest

from huddlehut import compact as C
from huddlehut.distill import signal_messages
from huddlehut.store import BoardError, Store


class FakeClient:
    def __init__(self, text="Decisions: summary v1"):
        self.calls = []
        self.text = text
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                               content=[SimpleNamespace(type="text", text=self.text)])


class Boom:
    beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("no key"))))


def view(store, key, client=None):
    return C.join_board(store.path, key, client=client or FakeClient())[0]


def test_view_builds_itself_from_message_types_without_a_model(store):
    board, key = store.create_board("parser", "repo", "a", goal="ship")
    store.post(key, "claude", "starting lexer; own src/lexer/*", "status")
    store.post(key, "codex", "writing tests", "status")
    store.post(key, "claude", "lexer done; now parser; own src/parser/*", "status")
    q = store.post(key, "codex", "spans on AST nodes?", "question")
    q2 = store.post(key, "codex", "CI image?", "question")
    store.post(key, "claude", "yes, spans everywhere", "answer", reply_to=q.id)
    store.post(key, "claude", "Pratt parser: simpler precedence", "decision")
    for i in range(5):
        store.post(key, "codex", f"chatter {i}", "note")
    store.post(key, "codex", "regex lexer was 4x slower", "deadend")

    client = FakeClient()
    out = view(store, key, client)
    assert client.calls == []
    now = out.split("Now:\n")[1].split("\n\n")[0] if "\n\n" in out else out.split("Now:\n")[1]
    assert "lexer done; now parser" in out and "starting lexer" not in out    # status superseded
    assert "writing tests" in out
    assert f"#{q2.id} codex/question: CI image?" in out.split("Open:")[1].split("Log:")[0]
    assert "spans on AST nodes?" in out.split("Log:")[1]                       # answered -> log
    assert f"re #{q.id}" in out
    assert "chatter 0" not in out and "chatter 4" in out and "2 older notes hidden" in out
    assert "regex lexer was 4x slower" in out and "Pratt parser" in out


def test_kind_aliases_and_unknown_kinds_dont_error(store):
    _, key = store.create_board("x", "p", "a")
    assert store.post(key, "a", "hi", "plan").kind == "status"
    assert store.post(key, "a", "hi", "update").kind == "note"
    assert store.post(key, "a", "hi", "whatever").kind == "note"
    with pytest.raises(BoardError):
        store.post(key, "a", "hi", "answer", reply_to=9999)


def test_compaction_only_when_log_outgrows_threshold_and_is_incremental(store):
    board, key = store.create_board("x", "p", "a")
    for i in range(40):
        store.post(key, "claude", "status ping " + "x" * 400, "status")  # statuses never need folding
    client = FakeClient()
    view(store, key, client)
    assert client.calls == []

    decisions = [store.post(key, "claude", f"decision {i}: " + "y" * 300, "decision") for i in range(25)]
    out = view(store, key, client)
    assert len(client.calls) == 1 and client.calls[0]["output_config"] == {"effort": "low"}
    prompt = client.calls[0]["messages"][0]["content"]
    assert "decision 0:" in prompt and "decision 24:" not in prompt and "status ping" not in prompt
    assert "Earlier:\nDecisions: summary v1" in out
    assert "decision 24:" in out and "decision 0:" not in out

    view(store, key, client)                       # second joiner reuses it
    assert len(client.calls) == 1

    for i in range(25):
        store.post(key, "codex", f"result {i}: " + "z" * 300, "result")
    client2 = FakeClient("summary v2")
    view(store, key, client2)
    p2 = client2.calls[0]["messages"][0]["content"]
    assert "summary v1" in p2 and "decision 0:" not in p2


def test_answer_to_folded_question_carries_snippet(store):
    board, key = store.create_board("x", "p", "a")
    q = store.post(key, "codex", "should we vendor the grammar?", "question")
    for i in range(25):
        store.post(key, "claude", f"decision {i}: " + "y" * 300, "decision")
    view(store, key)                                # folds; q stays open (not folded)
    store.post(key, "claude", "no, generate it at build time", "answer", reply_to=q.id)
    out = view(store, key)
    assert "Open:" not in out
    assert f"re #{q.id} (should we vendor the grammar?): no, generate it" in out


def test_failure_falls_back_to_capped_log_with_cooldown(store):
    board, key = store.create_board("x", "p", "a")
    for i in range(40):
        store.post(key, "claude", f"decision {i}: " + "y" * 300, "decision")
    out = view(store, key, Boom())
    assert "no key" in store.get_board(board.id).compact_error
    assert f"{40 - C.MAX_LOG} older log entries not yet summarized" in out
    assert "decision 39:" in out and "decision 0:" not in out
    client = FakeClient()
    view(store, key, client)
    assert client.calls == []                       # cooling down
    assert C.compact(store, board.id, client=client, force=True)


def test_one_compactor_at_a_time(store):
    board, key = store.create_board("x", "p", "a")
    for i in range(25):
        store.post(key, "claude", f"decision {i}: " + "y" * 300, "decision")
    assert store.claim_compaction(board.id)
    assert not C.compact(store, board.id, client=FakeClient())
    store.release_compaction(board.id)
    assert C.compact(store, board.id, client=FakeClient())


def test_distill_input_skips_chatter_and_superseded_statuses(store):
    board, key = store.create_board("x", "p", "a")
    store.post(key, "claude", "old status", "status")
    store.post(key, "claude", "final status", "status")
    store.post(key, "codex", "lol", "note")
    store.post(key, "codex", "use WAL mode " + "w" * 800, "decision")
    store.post(key, "codex", "pin base image " + "w" * 800, "decision")
    kept = [m.content[:12] for m in signal_messages(store, board.id)]
    assert kept == ["final status", "use WAL mode", "pin base ima"]


def test_distill_input_keeps_notes_when_nothing_was_typed(store):
    board, key = store.create_board("x", "p", "a")
    store.post(key, "claude", "found the bug in the retry loop", "note")
    assert [m.content for m in signal_messages(store, board.id)] == ["found the bug in the retry loop"]


def test_long_posts_rejected(store):
    _, key = store.create_board("x", "p", "a")
    with pytest.raises(BoardError, match="point to files"):
        store.post(key, "a", "x" * 1001)


def test_old_database_gets_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE boards (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL,"
               " project TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at REAL NOT NULL,"
               " status TEXT NOT NULL DEFAULT 'open', closed_by TEXT, closed_at REAL, outcome TEXT,"
               " distill_status TEXT, distill_error TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, board_id TEXT NOT NULL,"
               " author TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL)")
    db.commit()
    db.close()
    s = Store(path)
    board, key = s.create_board("x", "p", "a")
    q = s.post(key, "a", "q?", "question")
    assert s.post(key, "a", "yes", "answer", reply_to=q.id).reply_to == q.id
