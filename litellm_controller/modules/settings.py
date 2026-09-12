"""设置模块：查看与修改已有配置（LiteLLM 连接、Upstreams 增删改）。

交互约定与 models/routing 模块一致：数据行在上，操作选项在底部，Ctrl+C 返回。
"""
from InquirerPy import prompt
from InquirerPy.base.control import Choice

from .. import ui  # noqa: F401  导入即注册 DotFuzzy
from ..client import LiteLLMClient, LiteLLMError
from ..config import (
    ask_upstream_fields,
    fetch_known_providers,
    load_config,
    save_config,
)
from ..ui import FuzzySeparator

HINT_FUZZY = "输入筛选 · ↑↓/PgUp/PgDn 移动 · Home/End 首尾 · 回车 确认 · Ctrl+C 返回"


def ask(questions):
    try:
        return prompt(questions)
    except KeyboardInterrupt:
        return None


def _mask_key(key: str) -> str:
    if not key:
        return "(空)"
    if len(key) <= 10:
        return "****"
    return f"{key[:6]}…{key[-4:]}"


def _probe_litellm(endpoint: str, key: str) -> None:
    try:
        client = LiteLLMClient(endpoint, key, timeout=10)
        models = client.list_models()
        print(f"连接测试成功：当前可用模型 {len(models)} 个。")
    except (LiteLLMError, Exception) as e:
        print(f"连接测试失败: {e}")


def edit_litellm(config: dict) -> None:
    lit = config["litellm"]
    result = ask(
        [
            {
                "type": "input",
                "message": "LiteLLM endpoint:",
                "name": "endpoint",
                "default": lit["endpoint"],
                "validate": lambda val: bool(val and val.strip()),
                "invalid_message": "endpoint 不能为空",
            },
            {
                "type": "password",
                "message": "LiteLLM key（留空保持原 key）:",
                "name": "key",
            },
        ]
    )
    if not result:
        return
    endpoint = result["endpoint"].strip()
    key = (result.get("key") or "").strip() or lit["key"]

    print("\n======== LiteLLM 连接设置待保存预览 ========")
    print(f"Endpoint: {lit['endpoint']} -> {endpoint}")
    print(f"Key     : {'(未变更)' if not result.get('key') else '(已更新)'}")
    print("=" * 45)

    conf = ask(
        [{"type": "confirm", "message": "确认保存 LiteLLM 连接设置？", "name": "ok", "default": True}]
    )
    if not conf or not conf.get("ok"):
        print("已取消修改。")
        return

    config["litellm"]["endpoint"] = endpoint
    config["litellm"]["key"] = key
    save_config(config)
    print("LiteLLM 连接设置已成功保存。")
    _probe_litellm(endpoint, key)


def _name_conflict(config: dict, name: str, exclude_index: int = None) -> bool:
    return any(
        u["name"] == name for i, u in enumerate(config.get("upstreams") or []) if i != exclude_index
    )


def add_upstream(config: dict) -> None:
    lit = config["litellm"]
    known = fetch_known_providers(lit["endpoint"], lit["key"])
    new = ask_upstream_fields(lit["endpoint"], lit["key"], known_providers=known)
    if new is None:
        return
    if _name_conflict(config, new["name"]):
        print(f"Upstream 名称 [{new['name']}] 已存在，未保存。")
        return

    print("\n======== 待添加 Upstream 预览 ========")
    print(f"名称: {new['name']}")
    print(f"类型: {new['type']}")
    print(f"URL : {new['endpoint']}")
    print(f"Key : {_mask_key(new.get('key'))}")
    if new.get("provider"):
        print(f"绑定: {new['provider']}")
    print("=" * 35)

    conf = ask(
        [
            {
                "type": "confirm",
                "message": f"确认添加 Upstream [{new['name']}] 吗？",
                "name": "ok",
                "default": True,
            }
        ]
    )
    if not conf or not conf.get("ok"):
        print("已取消添加。")
        return

    config.setdefault("upstreams", []).append(new)
    save_config(config)
    print(f"Upstream [{new['name']}] 已添加并保存。")


def edit_upstream(config: dict, index: int) -> None:
    existing = config["upstreams"][index]
    lit = config["litellm"]
    known = fetch_known_providers(lit["endpoint"], lit["key"])
    new = ask_upstream_fields(
        lit["endpoint"], lit["key"], existing=existing, known_providers=known
    )
    if new is None:
        return
    if _name_conflict(config, new["name"], exclude_index=index):
        print(f"Upstream 名称 [{new['name']}] 已存在，未保存。")
        return

    print(f"\n======== Upstream [{existing['name']}] 修改预览 ========")
    has_diff = False
    for field in ["name", "type", "endpoint", "provider"]:
        old_v = existing.get(field)
        new_v = new.get(field)
        if old_v != new_v:
            has_diff = True
            print(f"{field:10}: {old_v} -> {new_v}")
    if existing.get("key") != new.get("key"):
        has_diff = True
        print(f"{'key':10}: (已更新)")
    if not has_diff:
        print("（参数无变更）")
    print("=" * 45)

    conf = ask([{"type": "confirm", "message": "确认保存以上修改？", "name": "ok", "default": True}])
    if not conf or not conf.get("ok"):
        print("已取消修改。")
        return

    config["upstreams"][index] = new
    save_config(config)
    print(f"Upstream [{new['name']}] 已更新并保存。")


def delete_upstream(config: dict, index: int) -> None:
    name = config["upstreams"][index]["name"]
    confirm = ask(
        [
            {
                "type": "confirm",
                "message": f"确认删除 Upstream [{name}]？",
                "name": "ok",
                "default": False,
            }
        ]
    )
    if not confirm or not confirm.get("ok"):
        return
    config["upstreams"].pop(index)
    save_config(config)
    print(f"Upstream [{name}] 已删除。")


def settings_module() -> None:
    config = load_config()
    while True:
        lit = config["litellm"]
        upstreams = config.get("upstreams") or []
        choices = [FuzzySeparator("─" * 70)]
        choices.append(
            Choice(
                "litellm",
                name=f"LiteLLM 连接: {lit['endpoint']}   key={_mask_key(lit['key'])}",
            )
        )
        choices.append(FuzzySeparator("─" * 70))
        if not upstreams:
            choices.append(FuzzySeparator("（未配置 Upstream）", indent=True))
        else:
            for i, u in enumerate(upstreams):
                tag = f" · provider:{u['provider']}" if u.get("provider") else ""
                choices.append(
                    Choice(
                        f"up:{i}",
                        name=f"Upstream: {u['name']:<24} {u['type']:<10} {u['endpoint']}{tag}",
                    )
                )
        choices.append(FuzzySeparator("─" * 70))
        choices.append(Choice("add_upstream", name="添加 Upstream"))
        choices.append(Choice("reset", name="重新配置并覆盖（清空现有配置）"))
        choices.append(Choice(None, name="[返回主菜单]"))
        result = ask(
            [
                {
                    "type": "fuzzy",
                    "message": "设置:",
                    "name": "selection",
                    "choices": choices,
                    "long_instruction": HINT_FUZZY,
                }
            ]
        )
        if not result or result["selection"] is None:
            return
        sel = result["selection"]
        if sel == "litellm":
            edit_litellm(config)
        elif sel == "add_upstream":
            add_upstream(config)
        elif sel == "reset":
            from ..config import _run_setup_flow

            confirm = ask(
                [
                    {
                        "type": "confirm",
                        "message": "重新配置将覆盖现有 config.yaml 中的全部设置，确定继续？",
                        "name": "ok",
                        "default": False,
                    }
                ]
            )
            if confirm and confirm.get("ok"):
                _run_setup_flow()
                try:
                    config = load_config()
                except ValueError as e:
                    print(f"配置无效: {e}")
                    return
        elif sel.startswith("up:"):
            index = int(sel.split(":", 1)[1])
            if index >= len(config.get("upstreams") or []):
                continue
            action = ask(
                [
                    {
                        "type": "list",
                        "message": f"管理 Upstream [{config['upstreams'][index]['name']}]:",
                        "name": "action",
                        "choices": [
                            Choice("edit", name="编辑此 Upstream"),
                            Choice("delete", name="删除此 Upstream"),
                            Choice(None, name="[返回]"),
                        ],
                        "long_instruction": "↑↓ 移动 · 回车 确认 · Ctrl+C 返回",
                    }
                ]
            )
            if action and action.get("action") == "edit":
                edit_upstream(config, index)
            elif action and action.get("action") == "delete":
                delete_upstream(config, index)
