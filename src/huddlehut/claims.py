"""Path helpers for the shared file inventory (claims table)."""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch


def normalize(path: str, toplevel: str | None = None, cwd: str | None = None) -> str:
    """A repo-relative path, so claims from different tools and folders line up.
    Relative paths are taken from cwd when given, otherwise from the repo root."""
    p = path.strip().strip("'\"")
    if cwd and not os.path.isabs(p):
        p = os.path.normpath(os.path.join(cwd, p))
    if toplevel and os.path.isabs(p):
        real_top = os.path.realpath(toplevel)
        for top in {toplevel, real_top}:
            if p == top:
                return "."
            if p.startswith(top.rstrip("/") + "/"):
                return p[len(top.rstrip("/")) + 1:]
        rp = os.path.realpath(p)
        if rp.startswith(real_top + "/"):
            return rp[len(real_top) + 1:]
    p = re.sub(r"^\./", "", p)
    return re.sub(r"/\*{1,2}$|/$", "/*", p) if p.endswith(("/", "/*", "/**")) else p


def _overlap(a: str, b: str) -> bool:
    """Same file, one inside the other's directory/glob, or a bare filename matching a full path."""
    a2, b2 = re.sub(r"/\*{1,2}$", "", a), re.sub(r"/\*{1,2}$", "", b)
    if a2 == b2 or a2.startswith(b2 + "/") or b2.startswith(a2 + "/"):
        return True
    if "/" not in a2 and b2.endswith("/" + a2) or "/" not in b2 and a2.endswith("/" + b2):
        return True
    return fnmatch(a, b) or fnmatch(b, a)


overlap = _overlap


def patch_paths(patch: str) -> list[str]:
    """Files touched by a Codex apply_patch payload."""
    return re.findall(r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+?)\s*$", patch, re.M)
