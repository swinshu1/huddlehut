from huddlehut.claims import claimed_paths, overlaps
from huddlehut.store import Message


def msg(i, author, content):
    return Message(id=i, board_id="b", author=author, kind="status", content=content, created_at=0)


def test_claimed_paths():
    assert claimed_paths("Pratt parser. Own src/parser/*, tests/test_lexer.py") == {"src/parser", "tests/test_lexer.py"}
    assert claimed_paths("refactor lexer.py and/or ast. see https://x.dev/a/b. 1/3 done") == {"lexer.py"}
    assert claimed_paths("thinking about approach") == set()


def test_overlaps_flag_other_agents_only():
    others = [
        msg(1, "codex-1", "tests. own tests/parser/**"),
        msg(2, "claude-2", "api. own src/api/routes.py"),
        msg(3, "me", "old status src/parser/x.py"),
    ]
    mine = msg(9, "me", "parser work: src/parser/*, tests/parser/test_ast.py")
    assert overlaps(mine, others) == ["codex-1 (#1) has tests/parser"]
    assert overlaps(msg(9, "me", "edit routes.py"), others) == ["claude-2 (#2) has src/api/routes.py"]
    assert overlaps(msg(9, "me", "docs/ only"), others) == []
