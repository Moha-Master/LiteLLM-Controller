#!/usr/bin/env python3
# --- LITELLMCTL CONFIG ---
NAME = "LiteLLM 官方模型价格（原版）"
DESCRIPTION = "从 BerriAI/litellm 官方仓库拉取原版 model_prices_and_context_window.json 作为基础层"
PRIORITY = 0
ENABLED = True
# -------------------------
"""LiteLLM 官方模型价格与上下文元数据脚本（litellm-controller 自带基础层）。

从 BerriAI/litellm 官方仓库获取原版 model_prices_and_context_window.json，
去除其中的非模型说明键（sample_spec / fallback_generalizations / __* 前缀），
把纯粹的“模型名 -> 参数字典”分片输出到 stdout，作为优先级 0 的基础层最先合并。

约定（litellm-controller 分片脚本要求）:
- stdout 只输出 JSON 分片（{"模型key": {字段...}}），日志一律走 stderr
- 失败时以非零退出码退出（构建会跳过本层并记录日志，不中断其它脚本合并）

单独运行:
    python litellm_official.py
"""
import json
import sys
import urllib.request

SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
TIMEOUT = 30

# 官方文件里并非模型的说明/特殊键，作为分片需剔除，
# 否则会被引擎当作模型条目并注入 litellm_provider。
SKIP_KEYS = {"sample_spec", "fallback_generalizations"}


def main():
    try:
        req = urllib.request.Request(
            SOURCE_URL, headers={"User-Agent": "litellm-controller/official-prices"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"拉取 LiteLLM 官方模型价格失败: {e}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict) or not payload:
        print("官方 model_prices_and_context_window.json 为空或格式异常", file=sys.stderr)
        return 1

    fragment = {
        k: v
        for k, v in payload.items()
        if not k.startswith("__") and k not in SKIP_KEYS and isinstance(v, dict)
    }

    print(
        f"LiteLLM 官方: 原始 {len(payload)} 键，输出 {len(fragment)} 个模型条目"
        f"（剔除 {len(payload) - len(fragment)} 个说明/特殊键），来源 {SOURCE_URL}",
        file=sys.stderr,
    )
    json.dump(fragment, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
