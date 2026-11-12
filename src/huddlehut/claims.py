"""Advisory file claims read from `status` posts, so overlapping work is flagged on the spot
instead of being negotiated in extra messages ("who owns src/api?")."""

from __future__ import annotations

import re
from fnmatch import fnmatch

from .store import Message

_EXT = r"(?:py|pyi|ts|tsx|js|jsx|mjs|go|rs|java|kt|rb|php|cs|c|h|cc|cpp|hpp|swift|md|json|toml|ya?ml|sql|sh|css|scss|html|vue|svelte|tf|proto)"
_NOT_PATHS = {"and/or", "n/a", "i/o", "w/o", "yes/no", "read/write", "client/server", "on/off", "input/output"}
_PATH = re.compile(rf"(?<![\w/.:-])((?:[\w.-]+/)+[\w.*-]*|[\w-]+\.{_EXT})(?![\w/])")


def claimed_paths(text: str) -> set[str]:
    """Paths/globs mentioned in a status, e.g. 'own src/parser/*, tests/test_lexer.py'."""
    out = set()
    for m in _PATH.finditer(text):
        p = m.group(1)
        if p.lower() in _NOT_PATHS or p.startswith(("http", "www.")) or not re.search(r"[A-Za-z]", p):
            continue
        p = re.sub(r"/\*{1,2}$|/$", "", p.strip(".,;:"))
        if p:
            out.add(p)
    return out


def _overlap(a: str, b: str) -> bool:
    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
        return True
    if "/" not in a and b.endswith("/" + a) or "/" not in b and a.endswith("/" + b):
        return True  # bare filename vs full path
    return fnmatch(a, b) or fnmatch(b, a)


def overlaps(mine: Message, current_statuses: list[Message]) -> list[str]:
    """Human-readable conflicts between my status and other agents' live statuses."""
    paths = claimed_paths(mine.content)
    if not paths:
        return []
    found = []
    for s in current_statuses:
        if s.author == mine.author:
            continue
        hit = sorted(p for p in claimed_paths(s.content) if any(_overlap(p, q) for q in paths))
        if hit:
            found.append(f"{s.author} (#{s.id}) has {', '.join(hit)}")
    return found
