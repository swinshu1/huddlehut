"""Trim success noise from posts: other agents need the outcome, not the numbers.

"474 pass, ruff+mypy clean" becomes "tests pass". Messages that mention any failure are left
alone, since then the numbers matter.
"""

from __future__ import annotations

import re

_FAILURE = re.compile(r"\b(fail\w*|error\w*|broke\w*|red|regress\w*|flak\w*|timeout|crash\w*)\b", re.I)
_EXTRA = r"(?:\s*[,;/]?\s*\d+\s+(?:skipped|xfailed|xpassed|deselected|warnings?|subtests?\s+passed))"
_TESTS = re.compile(
    r"\b(?:all\s+)?\d[\d,]*\s+(?:unit\s+|integration\s+|e2e\s+)?(?:tests?\s+)?(?:passed|passing|pass(?:es)?|green|ok)\b"
    + _EXTRA + r"*(?:\s+in\s+[\d.]+\s*m?s)?"
    r"|\ball\s+(?:unit\s+|integration\s+|e2e\s+)?tests?\s+(?:passed|passing|pass|green)\b",
    re.I,
)
_TOOLS = r"(?:ruff|mypy|pyright|pylint|flake8|black|isort|eslint|prettier|tsc|typecheck|types?|lint|clippy|golangci-lint|biome)"
_LINT = re.compile(
    rf"\b{_TOOLS}(?:\s*(?:\+|/|&|,|and)\s*{_TOOLS})*\s+(?:(?:are|is|all)\s+)?(?:clean|pass(?:es|ed|ing)?|ok|green|happy)\b",
    re.I,
)


def tidy(text: str) -> str:
    if _FAILURE.search(text):
        return text
    out = _TESTS.sub("tests pass", text)
    has_tests = "tests pass" in out
    # lint success next to passing tests adds nothing; on its own, keep it short
    out = _LINT.sub("\x00" if has_tests else "lint clean", out)
    if has_tests:
        out = re.sub(r"\s*[,;+&]?\s*(?:and\s+)?\x00", "", out)
        out = out.replace("\x00", "")
    out = re.sub(r"(tests pass)(?:\s*[,;]\s*tests pass)+", r"\1", out)  # "12 pass, all tests pass"
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"\(\s*\)", "", out).strip()
    return out or text
