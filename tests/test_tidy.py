import pytest

from huddlehut.tidy import tidy


@pytest.mark.parametrize("raw,clean", [
    ("474 pass, ruff+mypy clean", "tests pass"),
    ("Done. 474 passed, 3 skipped in 12.3s; ruff and mypy clean. Pushed a1b2c3d.", "Done. tests pass. Pushed a1b2c3d."),
    ("All 38 tests pass. lint clean", "tests pass."),
    ("ruff + mypy clean", "lint clean"),
    ("Refactored lexer (src/lexer.py). 120 tests passing, types ok.", "Refactored lexer (src/lexer.py). tests pass."),
    ("All tests passed; ready for review", "tests pass; ready for review"),
    ("added 3 seeds; 12 pass", "added 3 seeds; tests pass"),
])
def test_success_noise_is_trimmed(raw, clean):
    assert tidy(raw) == clean


@pytest.mark.parametrize("raw", [
    "470 passed, 4 failed: test_auth_*",
    "tsc clean but 2 errors in eslint",
    "milestone check red (5 cases)",
    "312 pass but 1 flaky (test_ws)",
    "Pratt parser. Precedence simpler.",
])
def test_failures_and_unrelated_text_untouched(raw):
    assert tidy(raw) == raw


def test_posts_are_tidied(store):
    _, key = store.create_board("b", "p", "a")
    assert store.post(key, "a", "snapshot rebuilt. 474 pass, ruff+mypy clean", "result").content == \
        "snapshot rebuilt. tests pass"


@pytest.mark.parametrize("raw", [
    'run: `d(){ [ -d "$1" ]; }; d /home && echo OK`',          # shell function in a code span
    "call foo() first, then bar( ) again",                      # written parentheses in prose
    "```\nx  =  1\ny() { :; }\n```",                            # code block: spacing and () kept
    "`pytest -q` gave `3 passed in 0.2s`",                      # numbers inside code are not trimmed
])
def test_code_and_written_parentheses_arrive_intact(raw):
    assert tidy(raw) == raw


def test_prose_is_still_tidied_around_code():
    assert tidy("ran `make check` (474 passed in 3.1s) and ruff clean") == "ran `make check` (tests pass)"
