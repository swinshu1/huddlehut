import json
import os
import stat

from huddlehut import install, store


def test_codex_hooks_merge_keeps_user_hooks_and_is_idempotent():
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 my_stop.py"}]}]}}
    once = install.codex_hooks_config(existing)
    twice = install.codex_hooks_config(once)
    assert once == twice                                                    # re-install doesn't duplicate
    stop_cmds = [h["command"] for g in twice["hooks"]["Stop"] for h in g["hooks"]]
    assert "python3 my_stop.py" in stop_cmds and any("stop --client codex" in c for c in stop_cmds)
    assert set(twice["hooks"]) == {"PreToolUse", "PostToolUse", "SessionStart", "Stop", "SessionEnd"}
    assert twice["hooks"]["PreToolUse"][-1]["matcher"] == "Bash|apply_patch"
    assert twice["hooks"]["SessionEnd"][-1]["hooks"][0]["timeout"] == 3


def test_config_file_selects_backend_and_is_private(tmp_path, monkeypatch):
    for var in ("HUDDLEHUT_DATABASE_URL", "HUDDLEHUT_DB", "CLAUDE_PLUGIN_OPTION_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(store, "CONFIG_FILE", cfg)
    monkeypatch.setattr(install, "CONFIG_FILE", cfg)
    assert install.set_database("postgresql://u:secret@db.example.com:5432/postgres") == "Postgres at db.example.com:5432"
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    assert "secret" not in install.describe()
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATABASE_URL", "postgresql://x@plugin.host/db")
    assert store.database_url() == "postgresql://x@plugin.host/db"           # plugin option beats file
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_DATABASE_URL")
    install.set_database("")
    assert install.describe().startswith("SQLite file")


def test_unset_plugin_options_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "CONFIG_FILE", tmp_path / "none.json")
    monkeypatch.delenv("HUDDLEHUT_DB", raising=False)
    monkeypatch.setenv("HUDDLEHUT_DATABASE_URL", "${user_config.database_url}")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATABASE_URL", "")
    assert store.database_url() == str(store.DEFAULT_SQLITE)
    assert store.env("HUDDLEHUT_DATABASE_URL") is None
