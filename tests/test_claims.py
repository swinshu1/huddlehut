import pytest

from huddlehut.claims import normalize, overlap, patch_paths


@pytest.mark.parametrize("a,b,hit", [
    ("src/api.py", "src/api.py", True),
    ("src/parser/lexer.py", "src/parser/*", True),
    ("src/parser/*", "src/parser/deep/x.py", True),
    ("lexer.py", "src/parser/lexer.py", True),
    ("src/api.py", "src/ui.py", False),
    ("src/api", "src/api_v2.py", False),
    ("tests/test_*.py", "tests/test_api.py", True),
])
def test_overlap(a, b, hit):
    assert overlap(a, b) is hit


def test_normalize():
    assert normalize("/repo/src/a.py", "/repo") == "src/a.py"
    assert normalize("./src/a.py") == "src/a.py"
    assert normalize("a.py", "/repo", cwd="/repo/src") == "src/a.py"
    assert normalize("src/parser/") == "src/parser/*"
    assert normalize("/elsewhere/x.py", "/repo") == "/elsewhere/x.py"


def test_patch_paths():
    patch = "*** Begin Patch\n*** Update File: src/api.py\n@@\n-x\n+y\n*** Add File: tests/t.py\n+z\n*** End Patch"
    assert patch_paths(patch) == ["src/api.py", "tests/t.py"]
