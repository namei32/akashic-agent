"""在停止的 Akashic 实例上，通过公共 writer 设置本插件的固定输入。"""

from __future__ import annotations

import argparse
from pathlib import Path

from agent.plugin_composition.config_input import load_config, save_config

from .plugin import Config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", required=True, type=Path, help="已安装插件的准确 plugin-data 目录"
    )
    parser.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--bridge-url")
    parser.add_argument("--token-env")
    args = parser.parse_args()
    current, _ = load_config(args.data_dir)
    changes = {
        key: value
        for key, value in {
            "enabled": args.enabled,
            "bridge_url": args.bridge_url,
            "token_env": args.token_env,
        }.items()
        if value is not None
    }
    config = Config.model_validate({**current, **changes})
    save_config(args.data_dir, config.model_dump())
    print("插件配置已保存；重启 Akashic 后使用新固定输入。")


if __name__ == "__main__":
    main()
