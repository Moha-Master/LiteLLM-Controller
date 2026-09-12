#!/usr/bin/env python3
# --- LITELLMCTL CONFIG ---
NAME = "OpenRouter 实时元数据"
DESCRIPTION = "从 OpenRouter 公共 API 拉取最新模型计费与上下文数据"
PRIORITY = 10
ENABLED = False
# -------------------------
"""OpenRouter 元数据生成脚本（litellm-controller 示例脚本）。

从 OpenRouter 公共 API 拉取模型价格/能力数据（无需 API key），
转换为 litellm model_prices_and_context_window.json 的分片格式输出到 stdout。

约定（litellm-controller auto 分片脚本要求）:
- stdout 只输出 JSON 分片（{"模型key": {字段...}}），日志一律走 stderr
- 失败时以非零退出码退出（构建将中止）

单独运行:
    python openrouter.py
"""
import json
import sys
import urllib.request

API_URL = "https://openrouter.ai/api/v1/models"
PROVIDER = "openrouter"
TIMEOUT = 30


def _float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main():
    try:
        req = urllib.request.Request(
            API_URL, headers={"User-Agent": "litellm-controller/openrouter"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"拉取 OpenRouter 模型列表失败: {e}", file=sys.stderr)
        return 1

    models = payload.get("data")
    if not isinstance(models, list) or not models:
        print("OpenRouter 返回了空的模型列表", file=sys.stderr)
        return 1

    fragment = {}
    skipped = 0
    for m in models:
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            skipped += 1
            continue
        arch = m.get("architecture") or {}
        in_mods = arch.get("input_modalities") or []
        out_mods = arch.get("output_modalities") or []
        # 只保留文本进/文本出的 chat 模型，过滤 embedding/图像/语音等
        if "text" not in in_mods or "text" not in out_mods:
            skipped += 1
            continue

        pricing = m.get("pricing") or {}
        in_cost = _float_or_none(pricing.get("prompt"))
        out_cost = _float_or_none(pricing.get("completion"))
        if in_cost is None or out_cost is None:
            skipped += 1
            continue

        entry = {
            "litellm_provider": PROVIDER,
            "mode": "chat",
            "input_cost_per_token": in_cost,
            "output_cost_per_token": out_cost,
        }

        cache_read = _float_or_none(pricing.get("input_cache_read"))
        if cache_read is not None:
            entry["cache_read_input_token_cost"] = cache_read

        ctx = m.get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            entry["max_input_tokens"] = ctx

        tp = m.get("top_provider") or {}
        max_out = tp.get("max_completion_tokens")
        if isinstance(max_out, int) and max_out > 0:
            entry["max_output_tokens"] = max_out
            entry["max_tokens"] = max_out

        params = m.get("supported_parameters") or []
        entry["supports_function_calling"] = "tools" in params
        entry["supports_vision"] = "image" in in_mods
        entry["supports_prompt_caching"] = cache_read is not None
        entry["supports_response_schema"] = (
            "response_format" in params or "structured_outputs" in params
        )

        fragment[mid] = entry

    print(
        f"OpenRouter: 共 {len(models)} 个模型，生成 {len(fragment)} 个 chat 模型条目，"
        f"跳过 {skipped} 个（非文本/缺价格）",
        file=sys.stderr,
    )
    json.dump(fragment, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
