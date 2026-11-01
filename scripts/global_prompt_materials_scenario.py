"""A real Context owner selects authorized global prompt contributions only."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.plugin_composition.config_input import save_config
from tests.test_default_reply import application

PROVIDER = '''
from agent.plugin_composition import ServiceKey
from plugins.context.contract import MATERIALS_V4
api_version = 3
name = "scenario_materials"
version = "1.0.0"
inject = (MATERIALS_V4,)
async def apply(ctx):
    calls = []
    async def trusted(snapshot, source):
        calls.append("trusted")
        return {"system_prompt": "TRUSTED_GLOBAL_INSTRUCTIONS", "reminders": (
            {"name": "private", "text": "PROMPT_OWNER_PRIVATE_REMINDER", "priority": 1},)}
    async def unscoped(snapshot, source):
        calls.append("unscoped")
        return {"reminders": ({"name": "other", "text": "UNSCOPED_CONTEXT_SECRET", "priority": 1},)}
    async def profile(snapshot, source):
        calls.append("profile")
        return {"reminders": ({"name": "profile", "text": "PROFILE_SECRET", "priority": 1},)}
    materials = ctx.require(MATERIALS_V4)
    await materials.register(ctx, name="trusted-global", kind="context", prepare=trusted, prompt=True)
    await materials.register(ctx, name="unscoped", kind="context", prepare=unscoped)
    await materials.register(ctx, name="profile", kind="profile", prepare=profile)
    await ctx.provide(ServiceKey("scenario.material-calls"), calls)
'''


async def scenario(folder: Path) -> None:
    from agent.plugin_composition import ServiceKey
    from plugins.content.plugin import check_text
    from plugins.context.contract import GLOBAL_PROMPT_MATERIALS, MATERIALS_V4
    from plugins.ledger.contract import ContentPart, Input
    from plugins.reply_program.contract import REPLY_EXECUTE_GLOBAL_PROMPT, REPLY_EXECUTE_V4

    def configure(sources: Path) -> None:
        plugin = sources / "scenario_materials"
        plugin.mkdir()
        (plugin / "plugin.py").write_text(PROVIDER)
        save_config(folder / "workspace/plugin-data/context-builtin", {
            "prompt_sources": {"trusted-global": "scenario_materials"},
        })

    async with application(folder, replying=False, extra_sources=configure) as (log, host):
        root = host.live_root
        assert root is not None
        writer = log.writer("scenario:target", author="user", source="conversation",
                            body_types=(Input,), content={"text": check_text})
        original = writer.append("original", Input((ContentPart("text", "A real user message"),)))
        writer.expire()
        messages = log.reader("scenario:target").snapshot()
        calls = root.context.require(ServiceKey("scenario.material-calls"))
        async with root.context.require(MATERIALS_V4).bind() as view:
            ordinary = await view.prepare(messages, "conversation")
        assert all(value in json.dumps(ordinary) for value in
                   ("TRUSTED_GLOBAL_INSTRUCTIONS", "PROMPT_OWNER_PRIVATE_REMINDER", "UNSCOPED_CONTEXT_SECRET", "PROFILE_SECRET"))
        calls.clear()
        async with root.context.require(GLOBAL_PROMPT_MATERIALS).bind() as view:
            restricted = await view.prepare(messages, "conversation")
        rendered = json.dumps(restricted)
        assert "TRUSTED_GLOBAL_INSTRUCTIONS" in rendered
        assert all(value not in rendered for value in ("PROMPT_OWNER_PRIVATE_REMINDER", "UNSCOPED_CONTEXT_SECRET", "PROFILE_SECRET"))
        assert calls == ["trusted"], calls
        assert log.reader("scenario:target").snapshot() == messages and original in messages
        assert root.context.get(REPLY_EXECUTE_GLOBAL_PROMPT) is not None
    older = folder / "older"
    older.mkdir()
    def old_context(sources: Path) -> None:
        module = sources / "context/plugin.py"
        text = module.read_text()
        start = text.index("    class GlobalPrompts:")
        module.write_text(text[:start])
    async with application(older, replying=False, extra_sources=old_context) as (_, host):
        root = host.live_root
        assert root is not None
        assert root.context.get(REPLY_EXECUTE_V4) is not None
        assert root.context.get(REPLY_EXECUTE_GLOBAL_PROMPT) is None
    print(json.dumps({"ordinary_materials_preserved": True, "only_global_prompt_called": True,
                      "unscoped_reminders_excluded": True, "original_messages_preserved": True,
                      "older_context_keeps_normal_reply": True}))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="gpm-") as directory:
        asyncio.run(scenario(Path(directory)))
