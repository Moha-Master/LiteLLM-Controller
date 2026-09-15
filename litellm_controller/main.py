"""命令行入口：非交互 metadata-gen 子命令 + Textual TUI 启动。"""
import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import get_config_dir, load_config
from .metadata import generate_metadata, sync_example_scripts


def _setup_logging():
    logging.getLogger().setLevel(logging.CRITICAL)
    for name in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def parse_args():
    parser = argparse.ArgumentParser(description="LiteLLM 管理工具（Textual TUI）")
    parser.add_argument(
        "-D",
        "--dir",
        help="配置文件目录路径",
        default=os.path.expanduser("~/.config/litellm-controller"),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"litellmctl {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    gen_parser = subparsers.add_parser(
        "metadata-gen",
        help="非交互生成 model_prices_and_context_window.json",
    )
    gen_parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="输出目录（可选，缺省使用 config.yaml 的 output_file，"
             "文件名固定为 model_prices_and_context_window.json）",
    )
    return parser.parse_args()


def _run_metadata_gen(args) -> int:
    try:
        config = load_config()
    except Exception as e:
        print(f"配置错误: {e}")
        return 1
    out_dir = Path(args.path).expanduser() if args.path else None
    try:
        generate_metadata(config, out_dir)
    except Exception as e:
        print(f"构建失败: {e}")
        return 1
    return 0


def main() -> int:
    _setup_logging()
    args = parse_args()
    os.environ["LITELLM_CONTROLLER_CONFIG_DIR"] = args.dir

    # 启动时自动同步示例脚本与默认 default.py
    try:
        get_config_dir()  # 确保配置目录可解析
        sync_example_scripts()
    except Exception:
        pass

    if args.command == "metadata-gen":
        return _run_metadata_gen(args)

    # 交互模式：交由 Textual App 处理配置向导与主界面
    from .app import LiteLLMControllerApp
    LiteLLMControllerApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
