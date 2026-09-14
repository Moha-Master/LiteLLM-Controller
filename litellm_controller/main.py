import argparse
import logging
import os
import sys
from pathlib import Path

from InquirerPy import prompt
from InquirerPy.base.control import Choice

from . import __version__
from .config import ensure_config_ready, load_config
from .modules.metadata import sync_example_scripts, generate_metadata
from .modules.models import models_module
from .modules.routing import routing_module
from .modules.settings import settings_module


def _setup_logging():
    logging.getLogger().setLevel(logging.CRITICAL)
    for name in ("urllib3", "requests", "InquirerPy", "charset_normalizer"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def parse_args():
    parser = argparse.ArgumentParser(description="LiteLLM 管理工具")
    parser.add_argument(
        "-D",
        "--dir",
        help="配置文件目录路径",
        default=os.path.expanduser("~/.config/litellm-controller"),
    )
    subparsers = parser.add_subparsers(dest="command")
    gen_parser = subparsers.add_parser(
        "metadata-gen",
        help="生成 model_prices_and_context_window.json",
    )
    gen_parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="输出目录（可选，缺省使用 config.yaml 的 output_file，"
             "文件名固定为 model_prices_and_context_window.json）",
    )
    return parser.parse_args()


def main():
    _setup_logging()
    args = parse_args()
    os.environ["LITELLM_CONTROLLER_CONFIG_DIR"] = args.dir
    if not ensure_config_ready():
        print("未完成配置，程序退出。")
        return 1

    # 启动时自动同步示例脚本与默认 default.py
    try:
        sync_example_scripts()
    except Exception:
        pass

    if args.command == "metadata-gen":
        config = load_config()
        out_dir = Path(args.path).expanduser() if args.path else None
        try:
            generate_metadata(config, out_dir)
        except Exception as e:
            print(f"构建失败: {e}")
            return 1
        return 0

    print(f"LiteLLM 管理工具 v{__version__}")
    print("=" * 30)
    while True:
        questions = [
            {
                "type": "list",
                "message": "请选择要执行的功能:",
                    "choices": [
                        Choice("manage_models", name="1. 模型管理"),
                        Choice("manage_routing", name="2. 路由管理"),
                        Choice("settings", name="3. 设置"),
                        Choice(value=None, name="[退出]"),
                    ],
                "name": "action",
                "long_instruction": "↑↓ 移动 · 回车 确认 · Ctrl+C 退出",
            }
        ]
        try:
            result = prompt(questions)
            if not result:
                print("\n已退出。")
                break
            action = result.get("action")
            if action == "manage_models":
                models_module()
            elif action == "manage_routing":
                routing_module()
            elif action == "settings":
                settings_module()
            elif action is None:
                print("\n已退出。")
                break
        except KeyboardInterrupt:
            print("\n已退出。")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
