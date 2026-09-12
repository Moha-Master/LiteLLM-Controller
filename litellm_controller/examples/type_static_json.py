#!/usr/bin/env python3
# --- LITELLMCTL CONFIG ---
NAME = "静态 JSON 字典示例"
DESCRIPTION = "纯 Python 字典内联静态元数据示例，可作为纯文本或定制化配置模版"
PRIORITY = 20
ENABLED = False
# -------------------------
"""静态 JSON 字典示例脚本。

可以直接在此脚本中内嵌特定模型的价格与上下文配置。
输出规范：将字典以 JSON 格式打印至标准输出（stdout），日志/提示输出至标准错误（stderr）。
"""
import json
import sys

DATA = {
    "my-custom-model": {
        "max_tokens": 4096,
        "max_input_tokens": 8192,
        "max_output_tokens": 4096,
        "input_cost_per_token": 0.000001,
        "output_cost_per_token": 0.000002,
        "litellm_provider": "openai",
        "mode": "chat",
        "supports_function_calling": True,
        "supports_vision": False,
    }
}


def main():
    json.dump(DATA, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
