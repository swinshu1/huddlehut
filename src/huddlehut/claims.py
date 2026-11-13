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


# ---- file names in posts ----------------------------------------------------
# The inventory already says who works on what, so posts shouldn't restate file names.

_EXT = (r"(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|rb|php|cs|c|h|cc|cpp|hpp|swift|md|mdx|json|toml|ya?ml|"
        r"sql|sh|css|scss|html|vue|svelte|tf|proto|txt|cfg|ini|lock|env|csv)")
_PATH_RE = rf"(?:[\w.-]+/)+(?:[\w.*-]*[\w*])?|[\w-]+\.{_EXT}"  # never ends in "." or ","
_PATH = re.compile(rf"(?<![\w/.:@-])({_PATH_RE})(?![\w/])")
_NOT_PATHS = {"and/or", "n/a", "i/o", "w/o", "yes/no", "read/write", "client/server", "on/off", "input/output"}
_LIST = r"\x00(?:\s*(?:,|;|&|\+|and)\s*\x00)*"
_LEAD = (r"(?:files?\s+(?:changed|touched|modified|edited)|changed|touched|modified|edited|updated|"
         r"own(?:s|ing)?|claim(?:s|ed|ing)?|editing|touching|working\s+on|in|on|to|for|of|at|via)")
_FILE_LIST = re.compile(
    rf"\b(?:files?\s+)?(?:changed|touched|modified|edited|updated)\s*:\s*(?:{_PATH_RE})(?:\s*(?:,|;|&|and)\s*(?:{_PATH_RE}))*",
    re.I,
)


def _is_path(p: str) -> bool:
    return p.lower() not in _NOT_PATHS and not p.startswith(("http", "www.")) and bool(re.search(r"[A-Za-z]", p))


def mentioned_paths(text: str) -> list[str]:
    """File paths and globs named in a post, e.g. 'own src/parser/*, tests/test_lexer.py'."""
    return list(dict.fromkeys(m.group(1).rstrip(".,;:") for m in _PATH.finditer(text) if _is_path(m.group(1))))


_OWNED = re.compile(
    rf"\b(?:i\s+)?(?:own(?:s|ing)?|claim(?:s|ing)?|my\s+files?:?|mine:)\s+((?:{_PATH_RE})(?:\s*(?:,|;|&|and)\s*(?:{_PATH_RE}))*)",
    re.I,
)


def owned_paths(text: str) -> list[str]:
    """Paths the author says are theirs: 'own src/parser/*, tests/x.py', 'claiming a.py'.
    Other mentions ('codex take src/ui') are not ownership and are left alone."""
    out = []
    for m in _OWNED.finditer(text):
        out += [p for p in mentioned_paths(m.group(1)) if p not in out]
    return out


def _tidy_gaps(t: str) -> str:
    t = re.sub(r"\(\s*[,;]?\s*\)", "", t)
    t = re.sub(r"\s+([,.;:!?)])", r"\1", t)
    t = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", t)
    t = re.sub(r"[,;:]\s*(?=[.!?]|$)", "", t)
    t = re.sub(r"(^|[.!?]\s+)[,;:]\s*", r"\1", t)
    t = re.sub(r"\.(?:\s*\.)+", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ,;:")


def strip_paths(text: str, drop) -> str:
    """Remove the paths for which drop(path) is true, with the words that only introduced them
    ('in', 'own', 'changed:') and any parentheses left empty."""
    t = _PATH.sub(lambda m: "\x00" if _is_path(m.group(1)) and drop(m.group(1).rstrip(".,;:")) else m.group(0), text)
    if "\x00" not in t:
        return text
    t = re.sub(rf"\(\s*{_LIST}\s*\)", "", t)
    t = re.sub(rf"\b{_LEAD}\s*:?\s*{_LIST}", "", t, flags=re.I)
    t = re.sub(_LIST, "", t)
    return _tidy_gaps(t)


def strip_file_lists(text: str) -> str:
    """Drop 'changed: a.py, b.py'-style lists from any post."""
    t = _FILE_LIST.sub("", text)
    return text if t == text else _tidy_gaps(t)
