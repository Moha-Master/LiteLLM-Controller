#!/usr/bin/env python3
# --- LITELLMCTL CONFIG ---
NAME = "DeepSeek 动态时段计费元数据"
DESCRIPTION = "根据北京时间动态输出 DeepSeek 模型的高峰/非高峰时段定价"
PRIORITY = 30
ENABLED = True
# -------------------------
"""DeepSeek 动态时段计费生成脚本。

模型：deepseek/deepseek-flash
逻辑：北京时间周一至周五 09:00-12:00, 14:00-18:00 为高峰时段（2x 价格），其余为谷价时段。
汇率：6.71 (CNY -> USD)
来源：https://api-docs.deepseek.com/quick_start/pricing
"""
import json
import sys
from datetime import datetime, timedelta, timezone

# 配置
MODEL_ID = "deepseek/deepseek-flash"
EXCHANGE_RATE = 6.71
# 谷价 (每 1M tokens, CNY)
OFF_PEAK_PRICING = {
    "input": 1.0,
    "output": 4.0,
    "cache_read": 0.02,
}

def is_peak_hour():
    # 获取北京时间 (UTC+8)
    tz_bj = timezone(timedelta(hours=8))
    now_bj = datetime.now(timezone.utc).astimezone(tz_bj)

    # 周末非高峰
    if now_bj.weekday() >= 5:  # 5: 周六, 6: 周日
        return False

    hour = now_bj.hour
    # 高峰时段：09:00-12:00, 14:00-18:00
    return bool(9 <= hour < 12 or 14 <= hour < 18)

# 四舍五入精度（小数位）。官方 model_prices_and_context_window.json 的干净值
# 集中在 6-9 位、最大 12 位，>=15 位为浮点误差。取 12 位可兼顾精度并规避误差尾巴。
ROUND_DIGITS = 12

def main():
    peak = is_peak_hour()

    # 换算为 USD/token（四舍五入到 ROUND_DIGITS 位）
    def to_usd_token(cny_1m):
        return round(cny_1m / EXCHANGE_RATE / 1_000_000, ROUND_DIGITS)

    # 先算谷价，峰价 = 谷价 * 2（保持精确 2x 关系）
    off_peak = {
        "input": to_usd_token(OFF_PEAK_PRICING["input"]),
        "output": to_usd_token(OFF_PEAK_PRICING["output"]),
        "cache_read": to_usd_token(OFF_PEAK_PRICING["cache_read"]),
    }
    on_peak = {k: round(v * 2, ROUND_DIGITS) for k, v in off_peak.items()}
    prices = on_peak if peak else off_peak

    entry = {
        "input_cost_per_token": prices["input"],
        "output_cost_per_token": prices["output"],
        "cache_read_input_token_cost": prices["cache_read"],
        "max_input_tokens": 1000000,
        "max_output_tokens": 393216,
        "max_tokens": 393216,
        "litellm_provider": "deepseek",
        "mode": "chat",
        "supports_vision": True,
        "supports_reasoning": True,
        "supports_function_calling": True,
        "supports_prompt_caching": True,
        "supports_response_schema": True,
        "supports_tool_choice": True,
        "supports_system_messages": True,
        "source": "https://api-docs.deepseek.com/quick_start/pricing",
    }

    result = {MODEL_ID: entry}

    # 打印日志到 stderr
    period_name = "高峰 (2x)" if peak else "谷价 (1x)"
    print(f"DeepSeek: 当前北京时间 {datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')}, "
          f"时段判定: {period_name}", file=sys.stderr)

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
