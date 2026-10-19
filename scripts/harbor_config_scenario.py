"""以 Harbor 当前配置启动真实宿主，并通过本地模型完成 CLI 回复。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(base: Path) -> None:
    """独占运行数据，验证启停选择、插件配置和实际回复链路。"""
    from benchmark.harbor_v4flash.prepare import prepare
    from agent.config import Config
    from agent.plugins.bundles import plugin_choices
    from bootstrap.app import AppRuntime
    from scripts.bundle_modes_scenario import headless_reply

    # 1. 使用 benchmark 的真实准备入口；模型服务复用既有隔离场景。
    workspace = base / "workspace"
    os.environ.update(HOME=str(base / "home"), AKASHIC_PLUGIN_HOME=str(base / "plugins"),
        AKASHIC_PLUGIN_DISTRIBUTION="", AKASHIC_EXTRA_PLUGIN_DIRS=str(ROOT / "plugins"),
        AKASHIC_EXECUTION_MODE="local",
        PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "sdk/python/src"))))
    prepare(workspace)
    config = ROOT / "benchmark/harbor_v4flash/config.toml"
    assert plugin_choices(workspace) == {"subagent": False, "computer": False}
    app = AppRuntime(Config.load(config, workspace=workspace), workspace)
    try:
        await app.start()
        manager = app.core.plugin_manager
        assert manager.generation("subagent") is None
        assert manager.generation("computer") is None
        assert manager.generation("reply").config_projection["max_steps"] == 0
        assert manager.generation("compaction").config_projection["keep_recent_tokens"] == 20000
        assert not manager.generation("akashic_clients").config_projection["enabled"]
        # 2. 真实 SDK 配置本地模型，CLI 通过 Gateway 接纳并读取回复。
        await headless_reply(app, workspace, config)
    finally:
        await app.shutdown()
    print("HARBOR_CURRENT_CONFIG_AND_CLI_REPLY_OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="harbor-config-") as directory:
        asyncio.run(run(Path(directory)))
