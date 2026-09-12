"""模型元数据管理模块：基于 Python 脚本的元数据源发现、优先级合并与可视化编辑。"""
import ast
import copy
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
from InquirerPy.base.control import Choice

from ..client import LiteLLMClient
from ..config import get_config_dir
from ..ui import FuzzySeparator

HINT_FUZZY = "输入筛选 · ↑↓/PgUp/PgDn 移动 · Home/End 首尾 · 回车 确认 · Ctrl+C 返回"

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
# 最终导出 JSON 的固定文件名
OUTPUT_FILENAME = "model_prices_and_context_window.json"

# ---------------------------------------------------------------- 脚本元数据解析

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
    enabled: Optional[bool] = None,
    priority: Optional[int] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
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
NAME = "默认元数据配置"
DESCRIPTION = "由可视化编辑器维护的本地模型元数据"
PRIORITY = 0
ENABLED = True
# -------------------------
\"\"\"由 litellmctl 可视化编辑器维护的本地模型定价与上下文元数据。\"\"\"
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
    2. 将内置 examples/*.py 复制到配置目录（默认禁用，不覆盖）
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
                # 示例脚本同步后强制设为禁用，由用户手动开启
                update_script_enabled(target, False)
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
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise MetadataError(f"读取基底文件 [upstream.json] 失败: {e}") from e
        else:
            return {}
        if not isinstance(data, dict):
            raise MetadataError("基底格式错误：必须是模型字典")
        return data

    def build(self) -> tuple[dict[str, Any], list[str]]:
        """构建最终的元数据字典。执行所有启用脚本，按优先级从小到大合并。"""
        logs = []
        final_map = {}
        
        # 1. Base: amend_upstream
        amend = self.config["model_metadata"].get("amend_upstream", {})
        if amend.get("type") != "off":
            logs.append(f"正在加载基底 amend_upstream ({amend['type']})...")
            try:
                base_data = self._fetch_amend_base()
                final_map.update(base_data)
                logs.append(f"  成功加载 {len(base_data)} 个基底条目")
            except Exception as e:
                logs.append(f"  加载基底失败: {e}")

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
            try:
                if os.name != "nt" and not os.access(meta.path, os.X_OK):
                    os.chmod(meta.path, 0o755)
                
                result = subprocess.run(
                    [sys.executable, str(meta.path)],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                data = json.loads(result.stdout)
                if not isinstance(data, dict):
                    logs.append(f"  脚本 [{meta.name}] 输出格式错误，跳过。")
                    continue
                
                # 冲突检测与合并
                for k, v in data.items():
                    if k in final_map:
                        prev_meta, prev_prio = model_origin.get(k, (None, -1))
                        if prev_prio == meta.priority:
                            logs.append(f"  警告: 模型 [{k}] 在同优先级 (PRIORITY={meta.priority}) 脚本 [{prev_meta.name if prev_meta else '基底'}] 和 [{meta.name}] 中重叠，已由后者覆盖")
                    
                    # 自动补全 litellm_provider (以脚本 stem 为准，除非脚本自定)
                    if isinstance(v, dict) and "litellm_provider" not in v:
                        v["litellm_provider"] = meta.path.stem
                    
                    model_origin[k] = (meta, meta.priority)
                
                deep_merge(final_map, data)
                logs.append(f"已执行 [{meta.name}] -> {len(data)} 个模型条目 (PRIORITY={meta.priority})")
                
            except Exception as e:
                logs.append(f"  脚本 [{meta.name}] 执行或解析失败: {e}")

        return final_map, logs

    def export(self, data: dict[str, Any], out_path: Optional[Path] = None) -> Path:
        if out_path is None:
            out_path = self.get_output_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = out_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(out_path)
        return out_path

    def get_diff(self, new_data: dict[str, Any]) -> list[str]:
        """与 Proxy 当前加载的 map 进行对比。"""
        try:
            current_map = self.client.fetch_model_cost_map()
        except Exception:
            return ["无法获取 Proxy 当前元数据，跳过对比。"]
        
        diffs = []
        all_keys = set(new_data.keys()) | set(current_map.keys())
        changed, added, removed = 0, 0, 0
        for k in all_keys:
            if k not in current_map:
                added += 1
            elif k not in new_data:
                removed += 1
            else:
                c_entry = current_map[k]
                n_entry = new_data[k]
                is_changed = False
                for f in ("input_cost_per_token", "output_cost_per_token", "max_input_tokens"):
                    if c_entry.get(f) != n_entry.get(f):
                        is_changed = True
                        break
                if is_changed:
                    changed += 1
        
        diffs.append("对比结果 (vs Proxy 当前加载):")
        diffs.append(f"  新增条目: {added}")
        diffs.append(f"  修改条目: {changed}")
        diffs.append(f"  移除条目: {removed} (Proxy 中原有但新 JSON 中缺失)")
        diffs.append(f"  最终总计: {len(new_data)} 条 (Proxy 当前为 {len(current_map)} 条)")
        return diffs

# ---------------------------------------------------------------- 命令行生成

def generate_metadata(config: dict, out_dir: Optional[Path] = None) -> Path:
    """执行构建流程并导出最终 JSON（litellmctl metadata-gen 命令，非交互）。

    out_dir: 目标目录（可选，仅目录，文件名固定）。
    为 None 时使用 config.yaml 的 model_metadata.output_file 目录
    （null 表示 $config_dir/model_metadata/）。
    """
    client = LiteLLMClient(config["litellm"]["endpoint"], config["litellm"]["key"])
    mgr = MetadataManager(client, config)

    print("\n" + "=" * 40)
    data, logs = mgr.build()
    for log in logs:
        print(log)

    print("-" * 20)
    for line in mgr.get_diff(data):
        print(line)
    print("-" * 20)

    if out_dir is None:
        out_path = mgr.export(data)
    else:
        out_path = mgr.export(data, out_dir / OUTPUT_FILENAME)
    print(f"成功导出至: {out_path}")
    print("=" * 40 + "\n")
    return out_path

# ---------------------------------------------------------------- UI 交互

def ask(questions):
    from InquirerPy import prompt
    try:
        return prompt(questions)
    except KeyboardInterrupt:
        return None

def metadata_module(client: LiteLLMClient, config: dict):
    mgr = MetadataManager(client, config)
    
    while True:
        choices = [
            Choice("build", name="1. 构建并导出元数据 (Build & Export)"),
            Choice("edit_default", name="2. 可视化编辑默认模型配置 (Edit default.py)"),
            Choice("manage_scripts", name="3. 元数据脚本管理 (Manage Metadata Scripts)"),
            Choice("inspect", name="4. 元数据预览与 Diff (Inspect & Diff)"),
            Choice(None, name="[返回模型管理]"),
        ]
        
        result = ask([{
            "type": "list",
            "message": "模型元数据管理:",
            "choices": choices,
            "name": "action",
            "long_instruction": "↑↓ 移动 · 回车 确认 · Ctrl+C 返回",
        }])
        
        if not result or result["action"] is None:
            break
        
        action = result["action"]
        if action == "build":
            do_build(mgr)
        elif action == "edit_default":
            edit_default_py(mgr)
        elif action == "manage_scripts":
            manage_scripts(mgr)
        elif action == "inspect":
            inspect_metadata(mgr)

def do_build(mgr: MetadataManager):
    print("\n" + "="*40)
    try:
        data, logs = mgr.build()
        for log in logs:
            print(log)
        
        diffs = mgr.get_diff(data)
        print("-" * 20)
        for d in diffs:
            print(d)
        
        print("-" * 20)
        c_res = ask([{
            "type": "confirm",
            "message": f"确认导出并覆盖到 {mgr.get_output_path()}？",
            "name": "ok",
            "default": True,
        }])
        if c_res and c_res.get("ok"):
            out_path = mgr.export(data)
            print(f"成功导出至: {out_path}")
            print("请确保 LiteLLM 已配置加载该文件并重启/重载。")
        else:
            print("已取消导出，未写入文件。")
    except Exception as e:
        print(f"构建失败: {e}")
    print("="*40 + "\n")
    try:
        input("按回车键继续...")
    except (KeyboardInterrupt, EOFError):
        pass

# ---------------------------------------------------------------- default.py 可视化编辑

def read_default_py_data(path: Path) -> dict:
    """运行 default.py 获取其当前 DATA 字典。如果文件不存在或异常则返回空字典。"""
    if not path.exists():
        return {}
    try:
        if os.name != "nt" and not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
        res = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True, timeout=15, check=True
        )
        data = json.loads(res.stdout)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def save_default_py(path: Path, data: dict):
    """保存模型字典回写至 default.py。保持头部配置。"""
    meta = parse_script_meta(path)
    header = format_header(meta.name, meta.description, meta.priority, meta.enabled)
    content = (
        f"#!/usr/bin/env python3\n"
        f"{header}\n"
        f'"""由 litellmctl 可视化编辑器维护的本地模型定价与上下文元数据。"""\n'
        f"import json\n"
        f"import sys\n\n"
        f"DATA = {json.dumps(data, ensure_ascii=False, indent=2)}\n\n\n"
        f"def main():\n"
        f"    json.dump(DATA, sys.stdout, ensure_ascii=False, indent=2)\n"
        f'    sys.stdout.write("\\n")\n'
        f"    return 0\n\n\n"
        f'if __name__ == "__main__":\n'
        f"    sys.exit(main())\n"
    )
    path.write_text(content, encoding="utf-8")

def edit_default_py(mgr: MetadataManager):
    """default.py 可视化编辑主流程。"""
    default_py_path = mgr.meta_dir / "default.py"
    if not default_py_path.exists():
        sync_example_scripts()
    
    data = read_default_py_data(default_py_path)
    old_data = copy.deepcopy(data)
    working_data = copy.deepcopy(data)
    
    while True:
        choices = [FuzzySeparator("─" * 70)]
        if not working_data:
            choices.append(FuzzySeparator("（暂无模型配置，可通过下方'添加模型'进行添加）", indent=True))
        else:
            hdr = f"{'Model Key':<35} {'配置情况':<25}"
            choices.append(FuzzySeparator(hdr, indent=True))
            for k in sorted(working_data.keys()):
                m_meta = working_data[k]
                parts = []
                in_cost = m_meta.get("input_cost_per_token")
                out_cost = m_meta.get("output_cost_per_token")
                if in_cost is not None:
                    parts.append(f"In:${in_cost * 1_000_000:.3f}")
                if out_cost is not None:
                    parts.append(f"Out:${out_cost * 1_000_000:.3f}")
                max_in = m_meta.get("max_input_tokens") or m_meta.get("max_tokens")
                if max_in is not None:
                    parts.append(f"Ctx:{max_in}")
                tiers = _collect_tiers(m_meta)
                if tiers:
                    parts.append("Tier:" + ",".join(f"{t}k" for t in tiers))
                cfg_str = f"[{' · '.join(parts)}]" if parts else "[未配置]"
                choices.append(Choice(k, name=f"{k:<35} {cfg_str}"))
        
        choices.append(FuzzySeparator("─" * 70))
        choices.append(Choice("__add__", name="+ 选择并添加模型 (Provider/Upstream/手动输入)..."))
        choices.append(FuzzySeparator("─" * 70))
        choices.append(Choice("__save__", name="保存修改 (Save)"))
        choices.append(Choice("__cancel__", name="取消 (Cancel)"))
        
        res = ask([{
            "type": "fuzzy",
            "message": f"可视化编辑 default.py (共 {len(working_data)} 个模型):",
            "choices": choices,
            "name": "action",
            "long_instruction": HINT_FUZZY,
        }])
        
        if not res or res["action"] == "__cancel__":
            return
        
        action = res["action"]
        if action == "__save__":
            added = len(set(working_data.keys()) - set(old_data.keys()))
            deleted = len(set(old_data.keys()) - set(working_data.keys()))
            changed = 0
            for k in set(old_data.keys()) & set(working_data.keys()):
                if old_data[k] != working_data[k]:
                    changed += 1
            
            print("\n" + "=" * 40)
            print(f"变动概览: 添加 {added} 个 / 删除 {deleted} 个 / 修改 {changed} 个")
            print("=" * 40)
            conf = ask([{"type": "confirm", "message": "确认保存修改至 default.py？", "name": "ok", "default": True}])
            if conf and conf.get("ok"):
                save_default_py(default_py_path, working_data)
                print("已成功保存至 default.py。")
                break
        elif action == "__add__":
            _add_models_to_dict(mgr, working_data)
        else:
            m_key = action
            updated, delete = _edit_single_model_metadata(m_key, working_data[m_key])
            if delete:
                del working_data[m_key]
            elif updated is not None:
                working_data[m_key] = updated

def _add_models_to_dict(mgr: MetadataManager, working_data: dict):
    from .models import fetch_upstream_models, fuzzy_picker
    
    choices = [
        Choice("builtin", name="从 LiteLLM 内置 Provider 数据拉取模型 (多选)"),
        Choice("upstream", name="从配置的 Upstream 实时拉取模型 (多选)"),
        Choice("manual", name="手动输入模型 Key (支持逗号分隔)"),
        Choice(None, name="[返回]"),
    ]
    
    res = ask([{
        "type": "list",
        "message": "请选择模型来源方式:",
        "choices": choices,
        "name": "source",
    }])
    if not res or not res["source"]:
        return
    
    source = res["source"]
    new_keys = []
    if source == "builtin":
        known_models = []
        try:
            cost_map = mgr.client.fetch_model_cost_map()
            known_models = sorted(cost_map.keys())
        except Exception:
            pass
        if known_models:
            picked = fuzzy_picker(known_models, f"选择要添加的模型 (共 {len(known_models)} 个已知模型):")
            if picked:
                new_keys = picked
        else:
            print("未能获取到 LiteLLM 内置模型列表。")
    elif source == "upstream":
        upstreams = mgr.config.get("upstreams") or []
        if not upstreams:
            print("配置中没有 Upstream。")
            return
        u_res = ask([{
            "type": "fuzzy",
            "message": "请选择 Upstream:",
            "choices": [Choice(u, name=f"{u['name']} ({u['type']})") for u in upstreams],
            "name": "u",
        }])
        if u_res and u_res["u"]:
            print("正在拉取模型列表...")
            try:
                models = fetch_upstream_models(u_res["u"])
                if models:
                    picked = fuzzy_picker(sorted(models), "选择要添加的模型:")
                    if picked:
                        new_keys = picked
                else:
                    print("列表为空。")
            except Exception as e:
                print(f"拉取失败: {e}")
    elif source == "manual":
        val_res = ask([{
            "type": "input",
            "message": "请输入模型 Key (例如 gpt-4o，多个可用英文逗号分隔):",
            "name": "keys",
            "validate": lambda v: bool(v and v.strip()),
        }])
        if val_res:
            new_keys = [k.strip() for k in val_res["keys"].split(",") if k.strip()]
            
    for k in new_keys:
        if k not in working_data:
            working_data[k] = {"mode": "chat"}

# ---------------------------------------------------------------- 脚本文件管理

def manage_scripts(mgr: MetadataManager):
    while True:
        script_files = sorted([
            f for f in mgr.meta_dir.glob("*.py")
            if not f.name.startswith(".")
        ], key=lambda f: f.name)
        
        choices = [
            FuzzySeparator("─" * 70),
            FuzzySeparator(f"{'名称':<22} {'脚本文件名':<20} {'优先级':<8} {'状态'}"),
            FuzzySeparator("─" * 70),
        ]
        
        for f in script_files:
            meta = parse_script_meta(f)
            status = "启用" if meta.enabled else "禁用"
            choices.append(Choice(
                f.name,
                name=f"{meta.name:<22} {f.name:<20} P={meta.priority:<6} [{status}]",
            ))
            
        choices.append(FuzzySeparator("─" * 70))
        choices.append(Choice("add_script", name="+ 新建脚本文件..."))
        choices.append(Choice("reset_all_examples", name="↺ 重新释放 / 重置所有内置示例脚本..."))
        choices.append(Choice(None, name="[返回]"))
        
        res = ask([{
            "type": "fuzzy",
            "message": f"模型元数据脚本管理 (共 {len(script_files)} 个脚本):",
            "choices": choices,
            "name": "sel",
            "long_instruction": HINT_FUZZY,
        }])
        
        if not res or res["sel"] is None:
            break
        
        sel = res["sel"]
        if sel == "add_script":
            add_script(mgr)
        elif sel == "reset_all_examples":
            conf = ask([{
                "type": "confirm",
                "message": "确认将所有内置示例脚本 (openrouter.py, deepseek.py, type_static_json.py) 重新释放覆盖？本地修改将被重置，并恢复为默认禁用状态。",
                "name": "ok",
                "default": False,
            }])
            if conf and conf.get("ok"):
                if EXAMPLES_DIR.exists():
                    for ex in EXAMPLES_DIR.glob("*.py"):
                        if not ex.name.startswith("."):
                            target = mgr.meta_dir / ex.name
                            target.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
                            update_script_enabled(target, False)
                            if os.name != "nt":
                                try:
                                    os.chmod(target, 0o755)
                                except OSError:
                                    pass
                    print("所有内置示例脚本已成功重置并初始化为禁用状态。")
        else:
            edit_script_item(mgr, mgr.meta_dir / sel)

def edit_script_item(mgr: MetadataManager, script_path: Path):
    while True:
        meta = parse_script_meta(script_path)
        print("\n" + "=" * 45)
        print(f"脚本管理: {meta.name} ({script_path.name})")
        print(f"说明: {meta.description}")
        print(f"优先级: {meta.priority}")
        print(f"当前状态: {'启用' if meta.enabled else '禁用'}")
        print("=" * 45)
        
        is_example = (EXAMPLES_DIR / script_path.name).is_file()
        
        choices = [
            Choice("toggle", name=f"{'禁用' if meta.enabled else '启用'}该脚本"),
            Choice("priority", name=f"调整优先级 (当前 PRIORITY={meta.priority})"),
        ]
        if script_path.name == "default.py":
            choices.append(Choice("edit_visual", name="进入可视化编辑器 (编辑 default.py)"))
        choices.append(Choice("preview", name="运行测试 / 预览脚本输出"))
        if is_example:
            choices.append(Choice("reset", name=f"重置为内置示例 ({script_path.name})"))
        if script_path.name != "default.py":
            choices.append(Choice("delete", name=f"删除脚本文件 ({script_path.name})"))
        choices.append(Choice(None, name="[返回]"))
        
        res = ask([{
            "type": "list",
            "message": "请选择操作:",
            "choices": choices,
            "name": "act",
        }])
        if not res or not res["act"]:
            break
        
        act = res["act"]
        if act == "toggle":
            new_state = not meta.enabled
            update_script_enabled(script_path, new_state)
            print(f"脚本 [{script_path.name}] 已{'启用' if new_state else '禁用'}。")
        elif act == "priority":
            p_res = ask([{
                "type": "input",
                "message": f"请输入脚本 [{script_path.name}] 的新优先级 (非负整数):",
                "default": str(meta.priority),
                "validate": lambda v: bool(v and v.strip().isdigit() and int(v.strip()) >= 0),
                "invalid_message": "请输入大于或等于 0 的整数",
                "name": "p",
            }])
            if p_res and p_res.get("p"):
                new_p = int(p_res["p"].strip())
                update_script_priority(script_path, new_p)
                print(f"优先级已修改为: {new_p}")
        elif act == "edit_visual":
            edit_default_py(mgr)
        elif act == "preview":
            preview_script_output(script_path)
        elif act == "reset":
            example_file = EXAMPLES_DIR / script_path.name
            conf = ask([{
                "type": "confirm",
                "message": f"确认将 {script_path.name} 重置为内置示例版本？这将覆盖本地改动并设为禁用。",
                "name": "ok",
                "default": False,
            }])
            if conf and conf.get("ok"):
                content = example_file.read_text(encoding="utf-8")
                script_path.write_text(content, encoding="utf-8")
                # 重置后默认禁用
                update_script_enabled(script_path, False)
                if os.name != "nt":
                    try:
                        os.chmod(script_path, 0o755)
                    except OSError:
                        pass
                print(f"脚本已成功重置为内置示例: {script_path.name}")
        elif act == "delete":
            conf = ask([{
                "type": "confirm",
                "message": f"确认永久删除脚本文件 {script_path.name}？",
                "name": "ok",
                "default": False,
            }])
            if conf and conf.get("ok"):
                script_path.unlink()
                print(f"文件已删除: {script_path.name}")
                break

def preview_script_output(script_path: Path):
    print("\n" + "=" * 50)
    print(f"运行脚本测试: {script_path.name}")
    try:
        if os.name != "nt" and not os.access(script_path, os.X_OK):
            os.chmod(script_path, 0o755)
        res = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True, text=True, timeout=60
        )
        if res.returncode != 0:
            print(f"脚本执行失败 (退出码 {res.returncode}):\n{res.stderr}")
        else:
            if res.stderr:
                print(f"Standard Error (日志输出):\n{res.stderr}")
            data = json.loads(res.stdout)
            print(f"Standard Output (输出 {len(data)} 个模型条目):\n" + json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"测试执行异常: {e}")
    print("=" * 50)
    try:
        input("按回车键继续...")
    except (KeyboardInterrupt, EOFError):
        pass

def add_script(mgr: MetadataManager):
    res_name = ask([{
        "type": "input",
        "message": "请输入新脚本名称 (如 my_provider，自动追加 .py):",
        "name": "name",
        "validate": lambda v: bool(v and v.strip() and re.match(r"^[A-Za-z0-9._-]+$", v.strip())),
        "invalid_message": "名称仅含字母、数字、. _ -",
    }])
    if not res_name or not res_name["name"]:
        return
    
    stem = res_name["name"].strip()
    stem = stem.removesuffix(".py")
    script_path = mgr.meta_dir / f"{stem}.py"
    if script_path.exists():
        print(f"脚本已存在: {script_path.name}")
        return
    
    choices = []
    if EXAMPLES_DIR.exists():
        for ex in sorted(EXAMPLES_DIR.glob("*.py")):
            if not ex.name.startswith("."):
                meta = parse_script_meta(ex)
                choices.append(Choice(ex.name, name=f"从示例: {meta.name} ({ex.name})"))
    choices.append(Choice("blank", name="空白 Python 脚本模版"))
    
    tpl_res = ask([{
        "type": "list",
        "message": f"选择脚本 [{stem}.py] 的创建模版:",
        "choices": choices,
        "name": "tpl",
    }])
    if not tpl_res or not tpl_res["tpl"]:
        return
    
    tpl = tpl_res["tpl"]
    if tpl == "blank":
        content = (
            f"#!/usr/bin/env python3\n"
            f"{format_header(stem, '自定义元数据脚本', 50, False)}\n"
            f"\"\"\"{stem} 元数据脚本。\"\"\"\n"
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
    else:
        ex_path = EXAMPLES_DIR / tpl
        content = ex_path.read_text(encoding="utf-8")
    
    conf = ask([{"type": "confirm", "message": f"确认创建脚本 {script_path.name}？", "name": "ok", "default": True}])
    if conf and conf.get("ok"):
        script_path.write_text(content, encoding="utf-8")
        update_script_enabled(script_path, False)
        if os.name != "nt":
            try:
                os.chmod(script_path, 0o755)
            except OSError:
                pass
        print(f"脚本已成功创建 (默认禁用状态): {script_path.name}")

def _cost_m_str(val) -> str:
    """per-token 价格 -> $/1M 输入框默认值（None 返回空串）。"""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        s = f"{val * 1_000_000:.6f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(val)


def _fmt_cost_m(val) -> str:
    """per-token 价格 -> $/1M 展示（None 返回 '-'）。"""
    s = _cost_m_str(val)
    return s if s else "-"


def _valid_nonneg_float(val: str) -> bool:
    if not val or not val.strip():
        return True
    try:
        return float(val) >= 0
    except ValueError:
        return False


def _valid_positive_int(val: str) -> bool:
    return bool(val and val.strip() and val.strip().isdigit() and int(val) > 0)


_TIER_FIELDS = (
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


def _collect_tiers(meta: dict) -> list:
    """收集条目中已设置的 above_{N}k_tokens 阶梯（按 N 升序）。"""
    tiers = set()
    for k in meta:
        m = _TIER_KEY_RE.match(k)
        if m:
            tiers.add(int(m.group(1)))
    return sorted(tiers)


def _edit_single_model_metadata(model_key: str, current_meta: dict):
    meta = copy.deepcopy(current_meta)

    while True:
        in_cost = meta.get("input_cost_per_token")
        out_cost = meta.get("output_cost_per_token")
        cache_read = meta.get("cache_read_input_token_cost")
        cache_creation = meta.get("cache_creation_input_token_cost")
        max_in = meta.get("max_input_tokens") or meta.get("max_tokens")
        max_out = meta.get("max_output_tokens")
        mode = meta.get("mode") or "chat"
        rpm = meta.get("rpm")
        tpm = meta.get("tpm")

        features = []
        for feat in ["supports_vision", "supports_function_calling", "supports_system_messages", "supports_prompt_caching", "supports_reasoning"]:
            if meta.get(feat) is True:
                features.append(feat.replace("supports_", ""))
        feat_str = ", ".join(features) if features else "无"

        tiers = _collect_tiers(meta)
        tier_str = ",".join(f"{t}k" for t in tiers) if tiers else "无"

        choices = [
            Choice("in_cost", name=f"1. Input Cost ($/1M)      : ${_fmt_cost_m(in_cost)}"),
            Choice("out_cost", name=f"2. Output Cost ($/1M)     : ${_fmt_cost_m(out_cost)}"),
            Choice("cache_read", name=f"3. Cache Read Cost ($/1M) : ${_fmt_cost_m(cache_read)}"),
            Choice("cache_creation", name=f"4. Cache Write Cost ($/1M): ${_fmt_cost_m(cache_creation)}"),
            Choice("max_in", name=f"5. Max Input Tokens    : {max_in if max_in is not None else '-'}"),
            Choice("max_out", name=f"6. Max Output Tokens   : {max_out if max_out is not None else '-'}"),
            Choice("mode", name=f"7. Mode / 类型         : {mode}"),
            Choice("features", name=f"8. 特性支持开关        : {feat_str}"),
            Choice("advanced", name=f"9. 更多高级字段 (阶梯:{tier_str} · rpm:{rpm if rpm is not None else '-'} · tpm:{tpm if tpm is not None else '-'}) >>"),
            Choice("delete", name="[从分片中移除该模型]"),
            FuzzySeparator("─" * 40),
            Choice("save", name="保存并返回 >>"),
            Choice("cancel", name="取消修改"),
        ]

        res = ask([{
            "type": "fuzzy",
            "message": f"配置模型 [{model_key}] 元数据:",
            "choices": choices,
            "name": "field",
        }])
        if not res or res["field"] == "cancel":
            return None, False

        f = res["field"]
        if f == "save":
            return meta, False
        if f == "delete":
            return None, True
        if f in ("in_cost", "out_cost", "cache_read", "cache_creation"):
            field_name = {
                "in_cost": "input_cost_per_token",
                "out_cost": "output_cost_per_token",
                "cache_read": "cache_read_input_token_cost",
                "cache_creation": "cache_creation_input_token_cost",
            }[f]
            label = {
                "in_cost": "Input", "out_cost": "Output",
                "cache_read": "Cache Read", "cache_creation": "Cache Write",
            }[f]
            v_res = ask([{
                "type": "input",
                "message": f"输入 {label} 价格 ($/1M tokens, 留空清除):",
                "default": _cost_m_str(meta.get(field_name)),
                "validate": _valid_nonneg_float,
                "invalid_message": "请输入非负数字或留空",
                "name": "v",
            }])
            if v_res is None:
                continue
            if v_res["v"].strip():
                meta[field_name] = float(v_res["v"]) / 1_000_000
            else:
                meta.pop(field_name, None)
        elif f in ("max_in", "max_out"):
            field_name = "max_input_tokens" if f == "max_in" else "max_output_tokens"
            cur_val = max_in if f == "max_in" else max_out
            v_res = ask([{
                "type": "input",
                "message": f"输入 Max {'Input' if f == 'max_in' else 'Output'} Tokens (留空清除):",
                "default": str(cur_val or ""),
                "validate": _valid_positive_int,
                "invalid_message": "请输入正整数或留空",
                "name": "v",
            }])
            if v_res is None:
                continue
            if v_res["v"].strip():
                meta[field_name] = int(v_res["v"])
                if f == "max_in":
                    meta.pop("max_tokens", None)
            else:
                meta.pop(field_name, None)
        elif f == "mode":
            m_res = ask([{
                "type": "list",
                "message": "选择 Mode:",
                "choices": ["chat", "completion", "embedding", "image_generation",
                            "audio_transcription", "audio_speech", "moderation",
                            "rerank", "search", "responses", "ocr", "realtime"],
                "default": mode if mode in ["chat", "completion", "embedding", "image_generation",
                                            "audio_transcription", "audio_speech", "moderation",
                                            "rerank", "search", "responses", "ocr", "realtime"] else "chat",
                "name": "v",
            }])
            if m_res:
                meta["mode"] = m_res["v"]
        elif f == "features":
            from .models import fuzzy_picker
            feat_keys = ["supports_vision", "supports_function_calling", "supports_system_messages", "supports_prompt_caching", "supports_reasoning", "supports_response_schema", "supports_audio_input"]
            choices_f = [Choice(fk, name=fk, enabled=meta.get(fk) is True) for fk in feat_keys]
            picked = fuzzy_picker(choices_f, "勾选支持的特性 (空格):")
            for fk in feat_keys:
                meta[fk] = fk in picked
        elif f == "advanced":
            _edit_advanced_meta(meta)

def _edit_tier(meta: dict, t: int):
    """管理某一阶梯 (input tokens > t*1000) 的四个价格。"""
    while True:
        rows = []
        for base, label in _TIER_FIELDS:
            v = meta.get(f"{base}_above_{t}k_tokens")
            rows.append(Choice(base, name=f"{label:<14} ($/1M): ${_fmt_cost_m(v)}"))
        rows.append(Choice("del_tier", name="[删除此阶梯全部价格]"))
        rows.append(Choice(None, name="[返回]"))
        res = ask([{
            "type": "fuzzy",
            "message": f"阶梯定价: 输入 tokens > {t}k 时整单适用以下价格:",
            "choices": rows,
            "name": "f",
        }])
        if not res or not res["f"]:
            break
        f = res["f"]
        if f == "del_tier":
            conf = ask([{
                "type": "confirm",
                "message": f"确认删除该模型 all above_{t}k_tokens 价格字段？",
                "name": "ok",
                "default": False,
            }])
            if conf and conf.get("ok"):
                for base, _ in _TIER_FIELDS:
                    meta.pop(f"{base}_above_{t}k_tokens", None)
                print(f"已删除 {t}k 阶梯。")
            break
        label = dict(_TIER_FIELDS)[f]
        v_res = ask([{
            "type": "input",
            "message": f"输入 {label} 价格 ($/1M tokens, 留空清除):",
            "default": _cost_m_str(meta.get(f"{f}_above_{t}k_tokens")),
            "validate": _valid_nonneg_float,
            "invalid_message": "请输入非负数字或留空",
            "name": "v",
        }])
        if v_res is None:
            continue
        if v_res["v"].strip():
            meta[f"{f}_above_{t}k_tokens"] = float(v_res["v"]) / 1_000_000
        else:
            meta.pop(f"{f}_above_{t}k_tokens", None)

def _new_tier(meta: dict):
    t_res = ask([{
        "type": "input",
        "message": "输入阶梯触发阈值 N (单位 k tokens, 如 200 表示输入超过 200k tokens 时启用):",
        "validate": _valid_positive_int,
        "invalid_message": "请输入正整数",
        "name": "t",
    }])
    if not t_res:
        return
    t = int(t_res["t"])
    if t in _collect_tiers(meta):
        print(f"{t}k 阶梯已存在，进入其管理。")
    else:
        print(f"提示: 触发键为 input_cost_per_token_above_{t}k_tokens (无此项该阶梯不会触发)，"
              "四个价格字段共享同一阈值 N。")
    _edit_tier(meta, t)

def _edit_advanced_meta(meta: dict):
    while True:
        choices = []
        for t in _collect_tiers(meta):
            trig = "可触发" if f"input_cost_per_token_above_{t}k_tokens" in meta else "缺触发键"
            choices.append(Choice(f"tier:{t}", name=f"阶梯定价 >{t}k tokens [{trig}] (管理)"))
        src = meta.get("source")
        src_str = (src[:50] + "…") if isinstance(src, str) and len(src) > 50 else (src or "-")
        choices.extend([
            Choice("new_tier", name="新增阶梯定价 (input_cost_per_token_above_Nk_tokens)"),
            Choice("rpm", name=f"RPM - 每分钟请求数: {meta.get('rpm') if meta.get('rpm') is not None else '-'}"),
            Choice("tpm", name=f"TPM - 每分钟 tokens: {meta.get('tpm') if meta.get('tpm') is not None else '-'}"),
            Choice("reasoning", name=f"Reasoning Output Cost ($/1M): ${_fmt_cost_m(meta.get('output_cost_per_reasoning_token'))}"),
            Choice("deprecation_date", name=f"Deprecation Date (YYYY-MM-DD): {meta.get('deprecation_date') or '-'}"),
            Choice("source", name=f"Source (定价来源 URL): {src_str}"),
            FuzzySeparator("─" * 40),
            Choice(None, name="[返回]"),
        ])
        res = ask([{"type": "fuzzy", "message": "高级字段:", "choices": choices, "name": "f"}])
        if not res or not res["f"]:
            break
        f = res["f"]
        if f.startswith("tier:"):
            _edit_tier(meta, int(f.split(":")[1]))
        elif f == "new_tier":
            _new_tier(meta)
        elif f in ("rpm", "tpm"):
            v_res = ask([{
                "type": "input",
                "message": f"输入 {f.upper()} (正整数, 留空清除):",
                "default": str(meta.get(f) or ""),
                "validate": _valid_positive_int,
                "invalid_message": "请输入正整数或留空",
                "name": "v",
            }])
            if v_res is None:
                continue
            if v_res["v"].strip():
                meta[f] = int(v_res["v"])
            else:
                meta.pop(f, None)
        elif f == "reasoning":
            v_res = ask([{
                "type": "input",
                "message": "输入 Reasoning Output 价格 ($/1M tokens, 留空清除):",
                "default": _cost_m_str(meta.get("output_cost_per_reasoning_token")),
                "validate": _valid_nonneg_float,
                "invalid_message": "请输入非负数字或留空",
                "name": "v",
            }])
            if v_res is None:
                continue
            if v_res["v"].strip():
                meta["output_cost_per_reasoning_token"] = float(v_res["v"]) / 1_000_000
            else:
                meta.pop("output_cost_per_reasoning_token", None)
        elif f == "deprecation_date":
            v_res = ask([{
                "type": "input",
                "message": "输入日期 (YYYY-MM-DD, 留空清除):",
                "default": meta.get("deprecation_date") or "",
                "name": "v",
            }])
            if v_res is None:
                continue
            meta["deprecation_date"] = v_res["v"].strip() or None
            if not meta["deprecation_date"]:
                meta.pop("deprecation_date", None)
        elif f == "source":
            v_res = ask([{
                "type": "input",
                "message": "输入定价来源 URL (留空清除):",
                "default": meta.get("source") or "",
                "name": "v",
            }])
            if v_res is None:
                continue
            if v_res["v"].strip():
                meta["source"] = v_res["v"].strip()
            else:
                meta.pop("source", None)

def inspect_metadata(mgr: MetadataManager):
    print("\n正在从配置的分片构建元数据以供预览...")
    try:
        data, _ = mgr.build()
        current_map = mgr.client.fetch_model_cost_map()
    except Exception as e:
        print(f"构建预览失败: {e}")
        return

    while True:
        choices = [FuzzySeparator("─" * 70)]
        for k in sorted(data.keys()):
            n_entry = data[k]
            c_entry = current_map.get(k, {})
            status = "新" if k not in current_map else "同"
            if status != "新":
                for f in ("input_cost_per_token", "output_cost_per_token", "max_input_tokens"):
                    if c_entry.get(f) != n_entry.get(f):
                        status = "变"
                        break
            
            in_cost = n_entry.get("input_cost_per_token")
            in_str = f"{in_cost * 1_000_000:.3f}" if isinstance(in_cost, (int, float)) else "-"
            out_cost = n_entry.get("output_cost_per_token")
            out_str = f"{out_cost * 1_000_000:.3f}" if isinstance(out_cost, (int, float)) else "-"
            
            tag = f"[{status}]"
            row_str = f"{tag:<4} {k:<30} In:${in_str:<8} Out:${out_str:<8}"
            choices.append(Choice(k, name=row_str))
            
        choices.append(FuzzySeparator("─" * 70))
        choices.append(Choice(None, name="[返回]"))
        
        res = ask([{
            "type": "fuzzy",
            "message": f"元数据预览 (共 {len(data)} 条模型，对比当前 Proxy):",
            "choices": choices,
            "name": "key",
            "long_instruction": HINT_FUZZY,
        }])
        if not res or not res["key"]:
            break
        
        k = res["key"]
        n_entry = data[k]
        c_entry = current_map.get(k, {})
        print(f"\n======== 模型元数据详情: {k} ========")
        print(f"新生成条目:\n{json.dumps(n_entry, ensure_ascii=False, indent=2)}")
        if c_entry:
            print(f"Proxy 当前实时条目:\n{json.dumps(c_entry, ensure_ascii=False, indent=2)}")
        else:
            print("Proxy 当前无此条目 (全新添加)")
        print("=" * 40 + "\n")
        try:
            input("按回车键继续...")
        except (KeyboardInterrupt, EOFError):
            pass
