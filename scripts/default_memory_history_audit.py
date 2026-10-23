"""Audit a supplied SQLite snapshot using the real current plugin pipeline."""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main(snapshot: Path, folder: Path) -> None:
    from agent.plugins.manager import PluginManager
    from agent.plugins.selection import PluginSelection
    from agent.plugin_composition.config_input import save_config
    from plugins.default_memory.contract import DEFAULT_MEMORY
    folder.mkdir(parents=True, exist_ok=False)
    os.chmod(folder, 0o700)
    workspace = folder / "workspace"
    workspace.mkdir()
    shutil.copy2(snapshot, workspace / "sessions.db")
    PluginSelection(workspace).initialize()
    # The disposable host uses builtin IDs; retain the production policy values.
    with sqlite3.connect(workspace / "sessions.db") as db:
        policies = db.execute("SELECT key,version,value FROM owner_records WHERE owner='plugin:akasha@release:scope-policy'").fetchall()
        for key, version, value in policies:
            db.execute("INSERT INTO owner_records VALUES (?,?,?,?)", ("plugin:akasha:scope-policy", key, version, value))
    sources = folder / "sources"
    for name in ("ledger", "commands", "content", "context", "tools", "turn_projection", "models", "akasha", "default_memory"):
        shutil.copytree(ROOT / "plugins" / name, sources / name, ignore=shutil.ignore_patterns("__pycache__"))
    save_config(workspace / "plugin-data/akasha-builtin", {"enabled": True, "memory_enabled": False})
    save_config(workspace / "plugin-data/default_memory-builtin", {"mode": "staging", "auto_learn": False})
    os.environ.update(HOME=str(folder / "home"), AKASHIC_PLUGIN_HOME=str(folder / "home"), AKASHIC_PLUGIN_DISTRIBUTION="")
    host = PluginManager([sources], workspace=workspace, installed_cache_root=folder / "home/cache")
    try:
        await host.load_all()
        port = host.live_root.context.require(DEFAULT_MEMORY)
        report = await port.audit()
        (folder / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"eligible_turns": report["eligible_turns"], "eligible_sessions_with_complete_turns": len(report["sessions"]),
                          "report": str(folder / "audit.json")}, ensure_ascii=False))
    finally:
        await host.terminate_all()
    with sqlite3.connect(workspace / "sessions.db") as copy:
        copy.execute("ATTACH DATABASE ? AS original", (str(snapshot),))
        for table in ("messages", "sessions", "attachments", "message_attachments"):
            difference = copy.execute(f"SELECT count(*) FROM (SELECT * FROM main.{table} EXCEPT SELECT * FROM original.{table})").fetchone()[0]
            missing = copy.execute(f"SELECT count(*) FROM (SELECT * FROM original.{table} EXCEPT SELECT * FROM main.{table})").fetchone()[0]
            if difference or missing:
                raise AssertionError((table, difference, missing))
        print("HISTORY_ALL_COLUMNS_IDENTICAL")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
