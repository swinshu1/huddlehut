"""Setup helpers: where boards are stored, and wiring huddlehut into Claude Code and Codex."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .store import CONFIG_FILE, database_url, is_postgres

ROOT = Path(__file__).resolve().parents[2]  # the checkout, which is also the Claude Code plugin
HOOK = ROOT / "scripts" / "hook"
CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"


def set_database(url: str | None) -> str:
    """Save the database URL to ~/.huddlehut/config.json (0600) for clients that can't pass env,
    such as Codex hooks. An empty URL means the local SQLite file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text() or "{}")
    if url:
        cfg["database_url"] = url
    else:
        cfg.pop("database_url", None)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_FILE, 0o600)
    return describe()


def describe() -> str:
    url = database_url()
    if is_postgres(url):
        host = url.split("@")[-1].split("/")[0].split("?")[0]
        return f"Postgres at {host}"
    return f"SQLite file {url}"


def codex_hooks_config(existing: dict) -> dict:
    """Merge huddlehut's hooks into a Codex hooks.json, replacing any earlier huddlehut entries."""
    cfg = dict(existing or {})
    hooks = cfg.setdefault("hooks", {})
    ours = {
        "PreToolUse": ("Bash|apply_patch", [{"type": "command", "command": f'"{HOOK}" pre-tool --client codex', "timeout": 10}]),
        "PostToolUse": (None, [{"type": "command", "command": f'"{HOOK}" post-tool --client codex', "timeout": 15}]),
        "SessionStart": (None, [{"type": "command", "command": f'"{HOOK}" session-start --client codex', "timeout": 15}]),
        "Stop": (None, [{"type": "command", "command": f'"{HOOK}" stop --client codex', "timeout": 15}]),
        # Codex allows SessionEnd hooks at most 3 seconds
        "SessionEnd": (None, [{"type": "command", "command": f'"{HOOK}" session-end --client codex', "timeout": 3}]),
    }
    for event, (matcher, handlers) in ours.items():
        groups = [
            g for g in hooks.get(event, [])
            if not any("huddlehut" in h.get("command", "") or str(HOOK) in h.get("command", "")
                       for h in g.get("hooks", []))
        ]
        groups.append({"matcher": matcher, "hooks": handlers} if matcher else {"hooks": handlers})
        hooks[event] = groups
    return cfg


def install_codex(dry_run: bool = False) -> list[str]:
    steps = []
    mcp = ["codex", "mcp", "add", "huddlehut", "--env", "HUDDLEHUT_AGENT=codex", "--",
           "uv", "run", "--quiet", "--directory", str(ROOT), "huddlehut-mcp"]
    steps.append(" ".join(mcp))
    existing = json.loads(CODEX_HOOKS.read_text()) if CODEX_HOOKS.exists() else {}
    merged = codex_hooks_config(existing)
    steps.append(f"write {CODEX_HOOKS} (huddlehut PreToolUse, PostToolUse, SessionStart, Stop, SessionEnd hooks)")
    if not dry_run:
        if not shutil.which("codex"):
            raise RuntimeError("codex CLI not found on PATH")
        subprocess.run(mcp, check=True)
        CODEX_HOOKS.parent.mkdir(parents=True, exist_ok=True)
        if CODEX_HOOKS.exists():
            shutil.copy(CODEX_HOOKS, CODEX_HOOKS.with_suffix(".json.bak"))
        CODEX_HOOKS.write_text(json.dumps(merged, indent=2))
    steps.append("then in Codex run /hooks and trust the huddlehut hooks (Codex skips untrusted hooks)")
    return steps


def install_claude(dry_run: bool = False) -> list[str]:
    cmds = [
        ["claude", "plugin", "marketplace", "add", str(ROOT)],
        ["claude", "plugin", "install", "huddlehut@huddlehut"],
    ]
    if not dry_run:
        if not shutil.which("claude"):
            raise RuntimeError("claude CLI not found on PATH")
        for c in cmds:
            subprocess.run(c, check=True)
    return [" ".join(c) for c in cmds] + [
        "set the database URL / API key when prompted, or later in /plugin (empty = local SQLite)"
    ]
