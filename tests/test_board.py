import threading
from types import SimpleNamespace

import pytest

from huddlehut.distill import Distillation, Lesson, distill
from huddlehut.store import BoardError, Store


def test_boards_are_isolated_and_key_gated(store):
    a, key_a = store.create_board("auth", "proj", "claude-code")
    b, key_b = store.create_board("billing", "proj", "codex")
    store.post(key_a, "claude-code", "touching auth/*")
    store.post(key_b, "codex", "touching billing/*")

    _, msgs_a = store.read(key_a)
    assert [m.content for m in msgs_a] == ["touching auth/*"]
    with pytest.raises(BoardError):
        store.read("brd_wrong")
    # key is never stored in plaintext
    assert key_a not in str([dict(r) for r in store.db.execute("SELECT * FROM boards")])


def test_late_joiner_sees_history_and_incremental_reads(store):
    _, key = store.create_board("x", "proj", "claude-code")
    m1 = store.post(key, "claude-code", "plan: refactor parser", "plan")
    store.post(key, "claude-code", "done with lexer", "result")
    # a second process opening the same file sees everything
    other = Store(store.path)
    _, msgs = other.read(key)
    assert [m.author for m in msgs] == ["claude-code", "claude-code"]
    _, newer = other.read(key, since_id=m1.id)
    assert [m.content for m in newer] == ["done with lexer"]


def test_wait_returns_when_another_agent_posts(store):
    _, key = store.create_board("x", "proj", "claude-code")
    threading.Timer(0.3, lambda: Store(store.path).post(key, "codex", "reply")).start()
    _, msgs = store.wait_for_messages(key, since_id=0, timeout=5, poll=0.1)
    assert msgs and msgs[0].author == "codex"


def test_spin_down_and_up(store):
    _, key = store.create_board("x", "proj", "claude-code")
    store.post(key, "claude-code", "hi")
    board = store.close_board(key, "claude-code", "shipped")
    assert board.status == "closed" and board.distill_status == "pending"
    with pytest.raises(BoardError):
        store.post(key, "codex", "too late")
    with pytest.raises(BoardError):
        store.close_board(key, "codex")
    board = store.reopen_board(key, "codex")
    assert board.status == "open"
    store.post(key, "codex", "back at it")


def test_unknown_kind_becomes_note(store):
    _, key = store.create_board("x", "proj", "a")
    assert store.post(key, "a", "hi", kind="nonsense").kind == "note"


class FakeClient:
    def __init__(self, lessons):
        self.calls = []
        parsed = Distillation(lessons=lessons)
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(parse=lambda **kw: self.calls.append(kw) or SimpleNamespace(
                stop_reason="end_turn", stop_details=None, parsed_output=parsed))
        )


LESSON = Lesson(
    title="Claim files on the board before editing",
    technique="Post the paths you will edit as a plan message before starting; others check it before touching code.",
    when_to_apply="Several agents working in the same repository at once",
    tags=["coordination", "merge-conflicts", "multi-agent"],
)


def test_distill_writes_searchable_memories_idempotently(store):
    _, key = store.create_board("x", "repo-a", "claude-code", goal="ship it")
    store.post(key, "claude-code", "I'll take src/api; codex take src/ui", "plan")
    board = store.close_board(key, "claude-code", "shipped")

    client = FakeClient([LESSON])
    distill(store, board.id, client=client)
    distill(store, board.id, client=client)  # re-distill replaces, doesn't duplicate

    assert store.get_board(board.id).distill_status == "done"
    hits = store.search_memories("avoid merge conflicts between agents")
    assert len(hits) == 1 and hits[0]["title"] == LESSON.title
    assert store.search_memories("conflicts", project="other-repo") == []
    prompt = client.calls[0]["messages"][0]["content"]
    assert "src/api" in prompt and "Goal: ship it" in prompt


def test_distill_failure_is_recorded(store):
    _, key = store.create_board("x", "p", "a")
    store.post(key, "a", "hi")
    board = store.close_board(key, "a")

    class Boom:
        beta = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))))

    with pytest.raises(RuntimeError):
        distill(store, board.id, client=Boom())
    b = store.get_board(board.id)
    assert b.distill_status == "failed" and "down" in b.distill_error
