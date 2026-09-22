"""模型参数元数据引擎：脚本发现、优先级合并、default.py 读写与非交互构建。

本模块为纯数据层，不含任何 UI 依赖；交互界面在 screens/meta.py。
"""
import ast
import copy
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .client import LiteLLMClient
from .config import get_config_dir

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
# 最终导出 JSON 的固定文件名
OUTPUT_FILENAME = "model_prices_and_context_window.json"


# ---------------------------------------------------------------- 脚本参数解析

@dataclass
class ScriptMeta:
    path: Path
    name: str
    description: str
    priority: int
    enabled: bool


HEADER_PREFIX = "# --- LITELLMCTL CONFIG ---"
HEADER_SUFFIX = "# -------------------------"
HEADER_RE = re.compile(
    r"# --- LITELLMCTL CONFIG ---.*?# -------------------------",
    re.DOTALL
)


def parse_script_meta(path: Path) -> ScriptMeta:
    """从脚本 AST 提取头部配置：NAME, DESCRIPTION, PRIORITY, ENABLED。若缺失提供默认值。"""
    default_name = path.stem
    default_desc = "无说明"
    default_priority = 0
    # default.py 默认启用，其它默认禁用
    default_enabled = (path.stem == "default")

    if not path.exists():
        return ScriptMeta(path, default_name, default_desc, default_priority, default_enabled)

    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
    except Exception:
        return ScriptMeta(path, default_name, default_desc, default_priority, default_enabled)

    name = default_name
    desc = default_desc
    priority = default_priority
    enabled = default_enabled

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    var = target.id
                    val = None
                    # Python 3.8+ Constant node
                    if isinstance(node.value, ast.Constant):
                        val = node.value.value

                    if var == "NAME" and isinstance(val, str):
                        name = val
                    elif var == "DESCRIPTION" and isinstance(val, str):
                        desc = val
                    elif var == "PRIORITY" and isinstance(val, int) and val >= 0:
                        priority = val
                    elif var == "ENABLED" and isinstance(val, bool):
                        enabled = val
    return ScriptMeta(path, name, desc, priority, enabled)


def format_header(name: str, description: str, priority: int, enabled: bool) -> str:
    return (
        f"{HEADER_PREFIX}\n"
        f"NAME = {json.dumps(name, ensure_ascii=False)}\n"
        f"DESCRIPTION = {json.dumps(description, ensure_ascii=False)}\n"
        f"PRIORITY = {priority}\n"
        f"ENABLED = {'True' if enabled else 'False'}\n"
        f"{HEADER_SUFFIX}"
    )


def update_script_meta_fields(
    path: Path,
    enabled: bool | None = None,
    priority: int | None = None,
    name: str | None = None,
    description: str | None = None,
):
    """更新脚本头部配置字段。"""
    if not path.exists():
        return
    meta = parse_script_meta(path)
    if enabled is not None:
        meta.enabled = enabled
    if priority is not None:
        meta.priority = priority
    if name is not None:
        meta.name = name
    if description is not None:
        meta.description = description

    content = path.read_text(encoding="utf-8")
    new_header = format_header(meta.name, meta.description, meta.priority, meta.enabled)
    if HEADER_RE.search(content):
        content = HEADER_RE.sub(new_header, content, count=1)
    else:
        lines = content.splitlines(keepends=True)
        idx = 1 if lines and lines[0].startswith("#!") else 0
        content = "".join(lines[:idx]) + new_header + "\n" + "".join(lines[idx:])
    path.write_text(content, encoding="utf-8")


def update_script_enabled(path: Path, enabled: bool):
    """更新脚本中的 ENABLED 状态。"""
    update_script_meta_fields(path, enabled=enabled)


def update_script_priority(path: Path, priority: int):
    """更新脚本中的 PRIORITY 优先级。"""
    update_script_meta_fields(path, priority=priority)


# ---------------------------------------------------------------- 默认模版与同步

DEFAULT_PY_TEMPLATE = """#!/usr/bin/env python3
# --- LITELLMCTL CONFIG ---
NAME = "默认参数配置"
DESCRIPTION = "由可视化编辑器维护的本地模型参数"
PRIORITY = 0
ENABLED = True
# -------------------------
\"\"\"由 litellmctl 可视化编辑器维护的本地模型定价与上下文参数。\"\"\"
import json
import sys

DATA = {}


def main():
    json.dump(DATA, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def sync_example_scripts():
    """在程序启动或 MetadataManager 初始化时：
    1. 生成默认启用且优先级为 0 的 default.py（如果不存在）
    2. 将内置 examples/*.py 复制到配置目录（沿用模板各自的 ENABLED 默认值，不覆盖）
    """
    config_dir = get_config_dir()
    meta_dir = config_dir / "model_metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)

    # 1. default.py
    default_py = meta_dir / "default.py"
    if not default_py.exists():
        default_py.write_text(DEFAULT_PY_TEMPLATE, encoding="utf-8")
        if os.name != "nt":
            try:
                os.chmod(default_py, 0o755)
            except OSError:
                pass

    # 2. Sync examples
    if EXAMPLES_DIR.exists():
        for f in EXAMPLES_DIR.glob("*.py"):
            if f.name.startswith("."):
                continue
            target = meta_dir / f.name
            if target.exists():
                continue
            try:
                content = f.read_text(encoding="utf-8")
                target.write_text(content, encoding="utf-8")
                # 模板头部的 ENABLED 即内置默认启用状态（demo 默认禁用，
                # 官方价格基础层默认启用），此处不再强制覆盖。
                if os.name != "nt":
                    try:
                        os.chmod(target, 0o755)
                    except OSError:
                        pass
            except Exception:
                continue


# ---------------------------------------------------------------- 核心引擎

class MetadataError(Exception):
    pass


def deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """字段级深合并模型条目字典。"""
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


class MetadataManager:
    def __init__(self, client: LiteLLMClient, config: dict):
        self.client = client
        self.config = config
        self.config_dir = get_config_dir()
        self.meta_dir = self.config_dir / "model_metadata"
        self.meta_dir.mkdir(parents=True, exist_ok=True)

        # 启动同步
        sync_example_scripts()

    def get_output_path(self) -> Path:
        """输出路径：output_file 为目录（null 表示 $config_dir/model_metadata/），
        文件名固定为 model_prices_and_context_window.json。"""
        path_str = self.config["model_metadata"].get("output_file")
        if path_str:
            out_dir = Path(path_str).expanduser()
        else:
            out_dir = self.meta_dir
        return out_dir / OUTPUT_FILENAME

    def _fetch_amend_base(self) -> dict[str, Any]:
        """按配置加载基底 (amend_upstream: off/url/file)。"""
        amend = self.config["model_metadata"].get("amend_upstream", {})
        a_type = amend.get("type", "off")
        if a_type == "url":
            url = amend.get("url")
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                raise MetadataError(f"从 URL [{url}] 获取基底失败: {e}") from e
        elif a_type == "file":
            file_path = self.meta_dir / "upstream.json"
            if not file_path.exists():
                return {}
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise MetadataError(f"读取基底文件 [upstream.json] 失败: {e}") from e
        else:
            return {}
        if not isinstance(data, dict):
            raise MetadataError("基底格式错误：必须是模型字典")
        return data

    def build(self) -> tuple[dict[str, Any], list[str]]:
        """构建最终的参数字典。执行所有启用脚本，按优先级从小到大合并。"""
        logs = []
        final_map = {}

        # 1. Base: amend_upstream
        amend = self.config["model_metadata"].get("amend_upstream", {})
        if amend.get("type") != "off":
            logs.append(f"正在加载基础列表 ({amend['type']})...")
            try:
                base_data = self._fetch_amend_base()
                final_map.update(base_data)
                logs.append(f"  成功加载 {len(base_data)} 个条目")
            except Exception as e:
                logs.append(f"  加载基础列表失败: {e}")

        # 2. Discover and sort enabled scripts
        scripts: list[ScriptMeta] = []
        for f in self.meta_dir.glob("*.py"):
            if f.name.startswith("."):
                continue
            meta = parse_script_meta(f)
            if meta.enabled:
                scripts.append(meta)

        # 按 PRIORITY 从小到大排序；优先级相同时按文件名排序以保证稳定性
        scripts.sort(key=lambda m: (m.priority, m.path.name))

        # 追踪模型来源与优先级，用于冲突警告
        model_origin: dict[str, tuple[ScriptMeta, int]] = {}

        for meta in scripts:
            data, error, stderr = run_script(meta.path, timeout=60)

            if error:
                logs.append(f"  脚本 [{meta.name}] {error}")
                if stderr:
                    logs.append(f"  详细信息:\n{stderr}")
                continue

            # 冲突检测与合并
            for k, v in data.items():
                if k in final_map:
                    prev_meta, prev_prio = model_origin.get(k, (None, -1))
                    if prev_prio == meta.priority:
                        logs.append(f"  警告: 模型 [{k}] 在同优先级 (PRIORITY={meta.priority}) 脚本 [{prev_meta.name if prev_meta else '基础列表'}] 和 [{meta.name}] 中重叠，已由后者覆盖")

                # 自动补全 litellm_provider (以脚本 stem 为准，除非脚本自定)
                if isinstance(v, dict) and "litellm_provider" not in v:
                    v["litellm_provider"] = meta.path.stem

                model_origin[k] = (meta, meta.priority)

            deep_merge(final_map, data)
            logs.append(f"已执行 [{meta.name}] -> {len(data)} 个模型条目 (PRIORITY={meta.priority})")

        return final_map, logs

    def export(self, data: dict[str, Any], out_path: Path | None = None) -> Path:
        if out_path is None:
            out_path = self.get_output_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = out_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(out_path)
        return out_path

    def get_diff_stats(self, new_data: dict[str, Any]) -> dict[str, Any]:
        """与 Proxy 当前加载的 map 对比，返回结构化统计。

        返回 {"ok": bool, "added": int, "changed": int, "removed": int, "total": int}。
        ok=False 表示无法获取 Proxy 当前参数。
        """
        try:
            current_map = self.client.fetch_model_cost_map()
        except Exception:
            return {"ok": False, "added": 0, "changed": 0, "removed": 0, "total": len(new_data)}

        added, changed, removed = 0, 0, 0
        for k in set(new_data.keys()) | set(current_map.keys()):
            if k not in current_map:
                added += 1
            elif k not in new_data:
                removed += 1
            else:
                c_entry = current_map[k]
                n_entry = new_data[k]
                for f in ("input_cost_per_token", "output_cost_per_token", "max_input_tokens"):
                    if c_entry.get(f) != n_entry.get(f):
                        changed += 1
                        break
        return {
            "ok": True,
            "added": added,
            "changed": changed,
            "removed": removed,
            "total": len(new_data),
        }


# ---------------------------------------------------------------- 脚本执行与 default.py 读写

def run_script(path: Path, timeout: int = 60) -> tuple[dict, str | None, str | None]:
    """运行 Python 脚本并返回其输出的 JSON 数据。

    返回 (data, error_message, stderr_output)：
    - 成功时返回 (数据字典, None, stderr 日志)
    - 失败时返回 ({}, 错误描述, stderr 输出)
    """
    if not path.exists():
        return {}, f"脚本文件不存在: {path}", None

    try:
        if os.name != "nt" and not os.access(path, os.X_OK):
            os.chmod(path, 0o755)

        res = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True, timeout=timeout
        )

        stderr_output = res.stderr.strip() if res.stderr else None

        if res.returncode != 0:
            return {}, f"脚本执行失败 (退出码 {res.returncode})", stderr_output

        data = json.loads(res.stdout)
        if not isinstance(data, dict):
            return {}, "脚本输出格式错误：期望字典但得到其他类型", stderr_output

        return data, None, stderr_output

    except json.JSONDecodeError as e:
        return {}, f"脚本输出的 JSON 格式错误: {e}", None
    except subprocess.TimeoutExpired:
        return {}, f"脚本执行超时 (超过 {timeout} 秒)", None
    except Exception as e:
        return {}, f"执行脚本时发生异常: {e}", None


def read_default_py_data(path: Path) -> tuple[dict, str | None]:
    """运行 default.py 获取其当前 DATA 字典。

    返回 (data, error_message)：
    - 成功时返回 (数据字典, None)
    - 失败时返回 ({}, 错误描述)
    """
    data, error, _ = run_script(path, timeout=15)
    return data, error


def _json_to_python_repr(obj, indent=2, _level=0):
    """将 Python 对象转换为 Python 源码格式的字符串（保留 True/False/None）。"""
    if obj is True:
        return "True"
    if obj is False:
        return "False"
    if obj is None:
        return "None"
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (int, float)):
        return repr(obj)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        items = []
        prefix = " " * indent * (_level + 1)
        for item in obj:
            items.append(prefix + _json_to_python_repr(item, indent, _level + 1))
        return "[\n" + ",\n".join(items) + "\n" + " " * indent * _level + "]"
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = []
        for k, v in obj.items():
            key_repr = json.dumps(k, ensure_ascii=False)
            val_repr = _json_to_python_repr(v, indent, _level + 1)
            items.append(" " * indent * (_level + 1) + f"{key_repr}: {val_repr}")
        return "{\n" + ",\n".join(items) + "\n" + " " * indent * _level + "}"
    return repr(obj)


def save_default_py(path: Path, data: dict):
    """保存模型字典回写至 default.py。保持头部配置。"""
    meta = parse_script_meta(path)
    header = format_header(meta.name, meta.description, meta.priority, meta.enabled)
    py_data_str = _json_to_python_repr(data)
    content = (
        f"#!/usr/bin/env python3\n"
        f"{header}\n"
        f'"""由 litellmctl 可视化编辑器维护的本地模型定价与上下文参数。"""\n'
        f"import json\n"
        f"import sys\n\n"
        f"DATA = {py_data_str}\n\n\n"
        f"def main():\n"
        f"    json.dump(DATA, sys.stdout, ensure_ascii=False, indent=2)\n"
        f'    sys.stdout.write("\\n")\n'
        f"    return 0\n\n\n"
        f'if __name__ == "__main__":\n'
        f"    sys.exit(main())\n"
    )
    path.write_text(content, encoding="utf-8")


BLANK_SCRIPT_TEMPLATE_STEM = "blank"


def blank_script_content(stem: str) -> str:
    return (
        f"#!/usr/bin/env python3\n"
        f"{format_header(stem, '自定义参数脚本', 50, False)}\n"
        f"\"\"\"{stem} 参数脚本。\"\"\"\n"
        f"import json\n"
        f"import sys\n\n"
        f"DATA = {{}}\n\n"
        f"def main():\n"
        f"    json.dump(DATA, sys.stdout, ensure_ascii=False, indent=2)\n"
        f'    sys.stdout.write("\\n")\n'
        f"    return 0\n\n"
        f'if __name__ == "__main__":\n'
        f"    sys.exit(main())\n"
    )


def reset_examples(meta_dir: Path) -> int:
    """将配置目录内的内置示例脚本重置为模板默认状态（含各自 ENABLED/PRIORITY），返回重置数量。"""
    count = 0
    if EXAMPLES_DIR.exists():
        for ex in EXAMPLES_DIR.glob("*.py"):
            if ex.name.startswith("."):
                continue
            target = meta_dir / ex.name
            target.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
            if os.name != "nt":
                try:
                    os.chmod(target, 0o755)
                except OSError:
                    pass
            count += 1
    return count


# ---------------------------------------------------------------- 展示/校验辅助（纯函数，供 UI 层复用）

def cost_m_str(val) -> str:
    """per-token 价格 -> $/1M 输入框默认值（None 返回空串）。"""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        s = f"{val * 1_000_000:.6f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(val)


def fmt_cost_m(val) -> str:
    """per-token 价格 -> $/1M 展示（None 返回 '-'）。"""
    s = cost_m_str(val)
    return s if s else "-"


def fmt_per_token(per_token) -> str:
    if per_token is None:
        return "-"
    return f"{per_token * 1e6:.4g}"


def valid_nonneg_float(val: str) -> bool:
    if not val or not val.strip():
        return True
    try:
        return float(val) >= 0
    except ValueError:
        return False


def valid_positive_int(val: str) -> bool:
    return bool(val and val.strip() and val.strip().isdigit() and int(val) > 0)


TIER_FIELDS = (
    ("input_cost_per_token", "Input"),
    ("output_cost_per_token", "Output"),
    ("cache_read_input_token_cost", "Cache Read"),
    ("cache_creation_input_token_cost", "Cache Write"),
)

_TIER_KEY_RE = re.compile(
    r"(?:input_cost_per_token|output_cost_per_token"
    r"|cache_read_input_token_cost|cache_creation_input_token_cost)"
    r"_above_(\d+)k_tokens$"
)


def collect_tiers(meta: dict) -> list:
    """收集条目中已设置的 above_{N}k_tokens 阶梯（按 N 升序）。"""
    tiers = set()
    for k in meta:
        m = _TIER_KEY_RE.match(k)
        if m:
            tiers.add(int(m.group(1)))
    return sorted(tiers)


BUILTIN_FEATURES = [
    "supports_vision", "supports_function_calling", "supports_tool_choice",
    "supports_system_messages", "supports_prompt_caching", "supports_reasoning",
    "supports_response_schema", "supports_audio_input",
]

MODE_CHOICES = [
    "chat", "completion", "embedding", "image_generation",
    "audio_transcription", "audio_speech", "moderation",
    "rerank", "search", "responses", "ocr", "realtime",
]

KNOWN_META_FIELDS = {
    "mode", "input_cost_per_token", "output_cost_per_token",
    "cache_read_input_token_cost", "cache_creation_input_token_cost",
    "max_input_tokens", "max_output_tokens", "max_tokens",
    "litellm_provider", "rpm", "tpm", "source", "deprecation_date",
    "output_cost_per_reasoning_token",
}


def config_summary(entry: dict) -> str:
    """default.py 条目的一行式配置摘要（[In:$x · Out:$y · Ctx:n · Tier:...]）。"""
    parts = []
    in_cost = entry.get("input_cost_per_token")
    out_cost = entry.get("output_cost_per_token")
    if in_cost is not None:
        parts.append(f"In:${in_cost * 1_000_000:.3f}")
    if out_cost is not None:
        parts.append(f"Out:${out_cost * 1_000_000:.3f}")
    max_in = entry.get("max_input_tokens") or entry.get("max_tokens")
    if max_in is not None:
        parts.append(f"Ctx:{max_in}")
    tiers = collect_tiers(entry)
    if tiers:
        parts.append("Tier:" + ",".join(f"{t}k" for t in tiers))
    return f"[{' · '.join(parts)}]" if parts else "[未配置]"


# ---------------------------------------------------------------- 命令行生成（非交互）

def generate_metadata(
    config: dict,
    out_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """执行构建流程并导出最终 JSON（litellmctl metadata-gen 命令，非交互）。

    out_dir: 目标目录（可选，仅目录，文件名固定）。
    为 None 时使用 config.yaml 的 model_metadata.output_file 目录
    （null 表示 $config_dir/model_metadata/）。
    """
    client = LiteLLMClient(config["litellm"]["endpoint"], config["litellm"]["key"])
    mgr = MetadataManager(client, config)

    log("=" * 40)
    data, logs = mgr.build()
    for entry in logs:
        log(entry)

    log("-" * 20)
    stats = mgr.get_diff_stats(data)
    if stats["ok"]:
        log(f"  新增条目: {stats['added']}")
        log(f"  修改条目: {stats['changed']}")
        log(f"  移除条目: {stats['removed']}")
        log(f"  最终总计: {stats['total']} 条")
    else:
        log("  无法获取 Proxy 当前参数，跳过对比。")
    log("-" * 20)

    if out_dir is None:
        out_path = mgr.export(data)
    else:
        out_path = mgr.export(data, out_dir / OUTPUT_FILENAME)
    log(f"成功导出至: {out_path}")
    log("=" * 40)
    return out_path
