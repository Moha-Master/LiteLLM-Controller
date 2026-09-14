"""模型管理模块：模型列表、编辑、禁用/启用、删除、添加模型向导。

交互约定：
- 操作提示统一放在各提示底部的 long_instruction 行，提示文本中不重复。
- 数据行在上，操作选项（含返回）统一放在底部，中间以分隔线隔开。
- Ctrl+C 返回上一级，各层级同时提供"返回/上一步"选项。
"""
from collections import Counter

from InquirerPy import prompt
from InquirerPy.base.control import Choice

from .. import ui  # noqa: F401  导入即注册 DotFuzzy
from ..client import LiteLLMClient, LiteLLMError
from ..config import load_config, cost_map_providers
from ..ui import FuzzySeparator
from ..upstreams import UpstreamError, fetch_upstream_models

ADD_MODEL_ACTION = "__add_model__"
MANUAL_CHOICE = "__manual__"

HINT_LIST = "↑↓ 移动 · 回车 确认 · Ctrl+C 返回"
HINT_FUZZY = "输入筛选 · ↑↓/PgUp/PgDn 移动 · Home/End 首尾 · 回车 确认 · Ctrl+C 返回"
HINT_MULTI = "输入筛选 · ↑↓/PgUp/PgDn 移动 · Home/End 首尾 · 空格 勾选 · 回车 确认 · Ctrl+C 返回"
HINT_CANCEL = "Ctrl+C 取消"

SORT_OPTIONS = {
    "public": "Public Name",
    "litellm": "LiteLLM Name",
    "provider": "Provider",
    "credential": "Credential",
    "status": "状态",
}

_cost_map_cache = None
_sort_key = "public"


def ask(questions):
    """prompt 封装：捕获 Ctrl+C（InquirerPy 会抛出 KeyboardInterrupt），统一视为取消当前步骤。"""
    try:
        return prompt(questions)
    except KeyboardInterrupt:
        return None


def _nonempty(val) -> bool:
    return bool(val and val.strip())


def _cost_validate(val) -> bool:
    val = (val or "").strip()
    if not val:
        return True
    try:
        return float(val) >= 0
    except ValueError:
        return False


def _separator(line: str = "─" * 100) -> FuzzySeparator:
    return FuzzySeparator(line)


def _current_first(current: str, choices: list, marker: str = "（当前）") -> list:
    """把当前值置顶并加标记，指针默认落在其上；输入框保持为空。"""
    if not current:
        return choices
    rest = [c for c in choices if c.value != current]
    return [Choice(current, name=f"{current}{marker}")] + rest


# ---------------------------------------------------------------- 基础数据


def get_cost_map(client: LiteLLMClient) -> dict:
    """获取 LiteLLM 内置模型列表（cost map），会话内缓存。"""
    global _cost_map_cache
    if _cost_map_cache is None:
        try:
            _cost_map_cache = client.fetch_model_cost_map()
        except LiteLLMError as e:
            print(f"获取内置模型列表失败: {e}")
            _cost_map_cache = {}
    return _cost_map_cache


def cost_map_models(cost_map: dict, provider: str) -> list:
    return sorted(
        key
        for key, value in cost_map.items()
        if isinstance(value, dict) and value.get("litellm_provider") == provider
    )


def fetch_credentials(client: LiteLLMClient) -> list:
    try:
        return client.list_credentials()
    except LiteLLMError as e:
        print(f"获取 Credential 列表失败: {e}")
        return []


def credential_rows(credentials: list):
    """Credential 列表的表格化行：名称 / provider / 掩码 key。"""
    if not credentials:
        return [FuzzySeparator("（无可用 Credential）")]
    name_w = max(len(str(c.get("credential_name", ""))) for c in credentials) + 2
    prov_w = max(
        len(str((c.get("credential_info") or {}).get("custom_llm_provider", "?")))
        for c in credentials
    )
    rows = []
    for c in credentials:
        info_c = c.get("credential_info") or {}
        values_c = c.get("credential_values") or {}
        name = str(c.get("credential_name", "?"))
        prov = str(info_c.get("custom_llm_provider", "?"))
        masked = values_c.get("api_key", "")
        rows.append(
            Choice(c.get("credential_name"), name=f"{name:<{name_w}} {prov:<{prov_w}} {masked}")
        )
    return rows


def model_id_of(model: dict) -> str:
    return (model.get("model_info") or {}).get("id") or model.get("model_name")


def _fmt_cost(per_token) -> str:
    if per_token is None:
        return "-"
    return f"{per_token * 1e6:.4g}"


def _cost_text(info: dict) -> str:
    return f"{_fmt_cost(info.get('input_cost_per_token'))}/{_fmt_cost(info.get('output_cost_per_token'))}"


def _clip(s, w) -> str:
    s = str(s)
    return s if len(s) <= w else s[: w - 1] + "…"


def _model_row(m: dict, widths: dict) -> str:
    info = m.get("model_info") or {}
    params = m.get("litellm_params") or {}
    pub = _clip(m.get("model_name", "?"), widths["pub"])
    ltm = _clip(params.get("model", "?"), widths["ltm"])
    prov = _clip(params.get("custom_llm_provider") or "-", widths["prov"])
    cost = _cost_text(info)
    cred = _clip(params.get("litellm_credential_name") or "-", widths["cred"])
    status = "🔴" if info.get("blocked") else "🟢"
    return (
        f"{pub:<{widths['pub']}} {ltm:<{widths['ltm']}} {prov:<{widths['prov']}} "
        f"{cost:<{widths['cost']}} {cred:<{widths['cred']}} {status}"
    )


def _table_widths(models: list) -> dict:
    def width(getter, cap):
        w = max([len(getter(m)) for m in models] + [4])
        return min(w, cap)

    return {
        "pub": width(lambda m: m.get("model_name", "?"), 26),
        "ltm": width(lambda m: (m.get("litellm_params") or {}).get("model", "?"), 28),
        "prov": width(lambda m: (m.get("litellm_params") or {}).get("custom_llm_provider") or "-", 14),
        "cost": 12,
        "cred": width(lambda m: (m.get("litellm_params") or {}).get("litellm_credential_name") or "-", 16),
    }


def _sorted_models(models: list, sort_key: str) -> list:
    def key(m):
        params = m.get("litellm_params") or {}
        info = m.get("model_info") or {}
        if sort_key == "litellm":
            return (params.get("model") or "",)
        if sort_key == "provider":
            return (params.get("custom_llm_provider") or "", m.get("model_name", ""))
        if sort_key == "credential":
            return (params.get("litellm_credential_name") or "", m.get("model_name", ""))
        if sort_key == "status":
            return (bool(info.get("blocked", False)), m.get("model_name", ""))
        return (m.get("model_name", ""),)

    return sorted(models, key=key)


# ---------------------------------------------------------------- 模型列表页


def models_module():
    global _sort_key
    config = load_config()
    client = LiteLLMClient(config["litellm"]["endpoint"], config["litellm"]["key"])
    while True:
        print("\n正在获取模型列表...")
        try:
            models = client.list_models()
        except KeyboardInterrupt:
            return
        except LiteLLMError as e:
            print(f"获取模型列表失败: {e}")
            return
        models = _sorted_models(models, _sort_key)
        widths = _table_widths(models)
        choices = [_separator()]
        if models:
            header = (
                f"{'Public Name':<{widths['pub']}} {'LiteLLM Name':<{widths['ltm']}} "
                f"{'Provider':<{widths['prov']}} {'Cost in/out':<{widths['cost']}} "
                f"{'Credential':<{widths['cred']}} 状态"
            )
            choices.append(FuzzySeparator(header, indent=True))
        name_counts = Counter(m.get("model_name", "?") for m in models)
        name_seen = Counter()
        for m in models:
            name = m.get("model_name", "?")
            name_seen[name] += 1
            row = _model_row(m, widths)
            if name_counts[name] > 1:
                row = f"{row}  [{name_seen[name]}/{name_counts[name]}]"
            choices.append(Choice(model_id_of(m), name=row))
        choices.append(_separator())
        choices.append(Choice(ADD_MODEL_ACTION, name="添加模型"))
        choices.append(Choice("metadata", name="模型参数管理"))
        choices.append(Choice("sort", name=f"排序设置 [{SORT_OPTIONS[_sort_key]}]"))
        choices.append(Choice("refresh", name="刷新模型列表"))
        choices.append(Choice(None, name="[返回主菜单]"))
        result = ask(
            [
                {
                    "type": "fuzzy",
                    "message": f"当前有 {len(models)} 个模型:",
                    "name": "selection",
                    "choices": choices,
                    "long_instruction": HINT_FUZZY,
                }
            ]
        )
        if result is None:
            return
        selection = result["selection"]
        if selection is None:
            return
        if selection == ADD_MODEL_ACTION:
            add_model_wizard(client, config)
            continue
        if selection == "metadata":
            from .metadata import metadata_module
            metadata_module(client, config)
            continue
        if selection == "sort":
            pick_sort()
            continue
        if selection == "refresh":
            continue
        model = next((m for m in models if model_id_of(m) == selection), None)
        if model is not None:
            model_actions(client, model)


def pick_sort():
    global _sort_key
    choices = [
        Choice(k, name=f"{v}（当前）" if k == _sort_key else v) for k, v in SORT_OPTIONS.items()
    ]
    result = ask(
        [
            {
                "type": "list",
                "message": "请选择排序方式:",
                "name": "sort",
                "choices": choices,
                "long_instruction": HINT_LIST,
            }
        ]
    )
    if result is None:
        return
    _sort_key = result["sort"]


# ---------------------------------------------------------------- 模型操作


def model_actions(client: LiteLLMClient, model: dict):
    while True:
        info = model.get("model_info") or {}
        params = model.get("litellm_params") or {}
        blocked = bool(info.get("blocked", False))
        name = model.get("model_name", "?")
        choices = [
            Choice("edit", name="编辑"),
            Choice("toggle", name="禁用" if not blocked else "启用"),
            Choice("delete", name="删除"),
            Choice(None, name="[返回模型列表]"),
        ]
        result = ask(
            [
                {
                    "type": "list",
                    "message": (
                        f"模型: {name}  |  provider: {params.get('custom_llm_provider', '?')}  |  "
                        f"credential: {params.get('litellm_credential_name', '无')}  |  "
                        f"cost: {_cost_text(info)}  |  状态: {'🔴 已禁用' if blocked else '🟢 启用中'}"
                    ),
                    "name": "action",
                    "choices": choices,
                    "long_instruction": HINT_LIST,
                }
            ]
        )
        if result is None:
            return
        action = result["action"]
        if action is None:
            return
        if action == "edit":
            edit_model(client, model)
        elif action == "toggle":
            set_blocked(client, model, not blocked)
        elif action == "delete":
            if delete_model(client, model):
                return


def edit_model(client: LiteLLMClient, model: dict) -> bool:
    model_id = model_id_of(model)
    info = model.get("model_info") or {}
    if not info.get("id"):
        print("该模型没有数据库 ID（model_info.id），无法编辑。")
        return False
    params = model.get("litellm_params") or {}
    try:
        cost_map = get_cost_map(client)
    except KeyboardInterrupt:
        return False
    cur_provider = params.get("custom_llm_provider") or ""
    cur_credential = params.get("litellm_credential_name") or ""
    provider_list = [Choice(p, name=p) for p in cost_map_providers(cost_map)]
    provider_list = _current_first(cur_provider, provider_list)
    provider_choices = [_separator()] + provider_list + [
        _separator(),
        Choice(MANUAL_CHOICE, name="手动输入"),
    ]
    credentials = fetch_credentials(client)
    credential_choices = []
    if credentials:
        rows = _current_first(cur_credential, credential_rows(credentials))
        credential_choices = [_separator()] + rows + [_separator()]
    credential_choices.append(Choice(MANUAL_CHOICE, name="手动输入"))
    answers = ask(
        [
            {
                "type": "input",
                "message": "Public Model Name:",
                "name": "model_name",
                "default": model.get("model_name", ""),
                "validate": _nonempty,
                "invalid_message": "Public Model Name 不能为空",
                "long_instruction": HINT_CANCEL,
            },
            {
                "type": "input",
                "message": "LiteLLM Model Name:",
                "name": "litellm_model",
                "default": params.get("model", ""),
                "validate": _nonempty,
                "invalid_message": "LiteLLM Model Name 不能为空",
                "long_instruction": HINT_CANCEL,
            },
            {
                "type": "fuzzy",
                "message": "Provider:",
                "name": "provider",
                "choices": provider_choices,
                "long_instruction": HINT_FUZZY,
            },
            {
                "type": "fuzzy",
                "message": "Credential:",
                "name": "credential",
                "choices": credential_choices,
                "long_instruction": HINT_FUZZY,
            },
            {
                "type": "input",
                "message": "Input cost（$/1M tokens，留空不修改）:",
                "name": "in_cost",
                "default": _fmt_cost(info.get("input_cost_per_token")) or "",
                "validate": _cost_validate,
                "invalid_message": "请输入非负数字",
                "long_instruction": HINT_CANCEL,
            },
            {
                "type": "input",
                "message": "Output cost（$/1M tokens，留空不修改）:",
                "name": "out_cost",
                "default": _fmt_cost(info.get("output_cost_per_token")) or "",
                "validate": _cost_validate,
                "invalid_message": "请输入非负数字",
                "long_instruction": HINT_CANCEL,
            },
            {
                "type": "input",
                "message": "Cache read cost（$/1M tokens，留空不修改）:",
                "name": "cache_read_cost",
                "default": _fmt_cost(info.get("cache_read_input_token_cost")) or "",
                "validate": _cost_validate,
                "invalid_message": "请输入非负数字",
                "long_instruction": HINT_CANCEL,
            },
            {
                "type": "input",
                "message": "Cache write cost（$/1M tokens，留空不修改）:",
                "name": "cache_write_cost",
                "default": _fmt_cost(info.get("cache_creation_input_token_cost")) or "",
                "validate": _cost_validate,
                "invalid_message": "请输入非负数字",
                "long_instruction": HINT_CANCEL,
            },
        ]
    )
    if answers is None:
        return False
    if answers["provider"] == MANUAL_CHOICE:
        manual = ask(
            [
                {
                    "type": "input",
                    "message": "请输入 Provider:",
                    "name": "provider",
                    "validate": _nonempty,
                    "invalid_message": "provider 不能为空",
                    "long_instruction": HINT_CANCEL,
                }
            ]
        )
        if manual is None:
            return False
        provider = manual["provider"].strip()
    else:
        provider = answers["provider"]
    if answers["credential"] == MANUAL_CHOICE:
        manual = ask(
            [
                {
                    "type": "input",
                    "message": "请输入 Credential 名称（可留空表示不绑定）:",
                    "name": "credential",
                    "long_instruction": HINT_CANCEL,
                }
            ]
        )
        if manual is None:
            return False
        credential = manual["credential"].strip()
    else:
        credential = answers["credential"] or ""
    cost_map_fields = {
        "in_cost": "input_cost_per_token",
        "out_cost": "output_cost_per_token",
        "cache_read_cost": "cache_read_input_token_cost",
        "cache_write_cost": "cache_creation_input_token_cost",
    }
    model_info = {}
    for qname, field in cost_map_fields.items():
        val = (answers.get(qname) or "").strip()
        if val:
            model_info[field] = round(float(val) / 1e6, 12)
    confirm = ask(
        [
            {
                "type": "confirm",
                "message": (
                    f"确认修改？\n  model name: {answers['model_name'].strip()}\n"
                    f"  litellm model: {answers['litellm_model'].strip()}\n"
                    f"  provider: {provider}\n  credential: {credential or '无'}\n"
                    f"  cost: {_cost_display(model_info)}"
                ),
                "name": "ok",
                "default": True,
            }
        ]
    )
    if not confirm or not confirm.get("ok"):
        print("已取消。")
        return False
    litellm_params = {
        "model": answers["litellm_model"].strip(),
        "custom_llm_provider": provider,
    }
    if credential:
        litellm_params["litellm_credential_name"] = credential
    try:
        fields = {
            "model_name": answers["model_name"].strip(),
            "litellm_params": litellm_params,
        }
        if model_info:
            fields["model_info"] = model_info
        client.update_model(model_id, **fields)
        print(f"模型已更新: {answers['model_name'].strip()}")
        # 就地刷新内存中的模型数据，保证操作页状态同步
        model["model_name"] = answers["model_name"].strip()
        model.setdefault("litellm_params", {}).update(litellm_params)
        model.setdefault("model_info", {}).update(model_info)
        return True
    except LiteLLMError as e:
        print(f"更新失败: {e}")
        return False


def _cost_display(model_info: dict) -> str:
    if not model_info:
        return "不修改"
    label_map = {
        "input_cost_per_token": "in",
        "output_cost_per_token": "out",
        "cache_read_input_token_cost": "cache read",
        "cache_creation_input_token_cost": "cache write",
    }
    return ", ".join(f"{label_map[k]} {_fmt_cost(v)}" for k, v in model_info.items())


def set_blocked(client: LiteLLMClient, model: dict, blocked: bool):
    mid = model_id_of(model)
    if not (model.get("model_info") or {}).get("id"):
        print("该模型没有数据库 ID（model_info.id），无法禁用/启用。")
        return
    action = "禁用" if blocked else "启用"
    name = model.get("model_name", "?")

    confirm = ask(
        [
            {
                "type": "confirm",
                "message": f"确认{action}模型 [{name}]？",
                "name": "ok",
                "default": True,
            }
        ]
    )
    if not confirm or not confirm.get("ok"):
        print(f"已取消{action}。")
        return

    try:
        client.update_model(mid, blocked=blocked)
        print(f"模型 {name} 已{action}。")
        model.setdefault("model_info", {})["blocked"] = blocked
    except LiteLLMError as e:
        print(f"{action}失败: {e}")


def delete_model(client: LiteLLMClient, model: dict) -> bool:
    model_id = (model.get("model_info") or {}).get("id")
    name = model.get("model_name", "?")
    if not model_id:
        print("该模型没有数据库 ID（model_info.id），无法删除。")
        return False
    confirm = ask(
        [
            {
                "type": "confirm",
                "message": f"确认删除模型 {name}？此操作不可恢复。",
                "name": "ok",
                "default": False,
            }
        ]
    )
    if not confirm or not confirm.get("ok"):
        return False
    try:
        client.delete_model(model_id)
        print(f"模型 {name} 已删除。")
        return True
    except LiteLLMError as e:
        print(f"删除失败: {e}")
        return False


# ---------------------------------------------------------------- 添加模型向导


def add_model_wizard(client: LiteLLMClient, config: dict):
    state = {"provider": None, "models": [], "mappings": {}, "credential": None, "focus": None}
    step = 1
    jump_back_to_review = False
    while True:
        if step == 5:
            nxt = step_review(state, client)
            if nxt is None:
                return
            if nxt >= 6:
                return
            if nxt == 5:
                continue
            step = nxt
            jump_back_to_review = True
            continue
        if step == 1:
            nxt = step_provider(state, client)
        elif step == 2:
            nxt = step_models(state, client, config)
        elif step == 3:
            nxt = step_mappings(state)
        else:
            nxt = step_credential(state, client)
        if nxt is None:
            return
        if jump_back_to_review:
            # 统一操作逻辑：从确认页跳转进入的步骤，完成后一律回到确认页
            nxt = 5
            jump_back_to_review = False
        step = nxt


def step_provider(state: dict, client: LiteLLMClient):
    try:
        cost_map = get_cost_map(client)
    except KeyboardInterrupt:
        return None
    providers = cost_map_providers(cost_map)
    choices = [_separator()]
    choices.extend(Choice(p, name=p) for p in providers)
    choices.append(_separator())
    choices.append(Choice(MANUAL_CHOICE, name="手动输入"))
    choices.append(Choice(None, name="[取消]"))
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": f"请选择 Custom Provider:",
                "name": "provider",
                "choices": choices,
                "long_instruction": HINT_FUZZY,
            }
        ]
    )
    if result is None:
        return None
    provider = result["provider"]
    if provider is None:
        return None
    if provider == MANUAL_CHOICE:
        manual = ask(
            [
                {
                    "type": "input",
                    "message": "请输入 Provider:",
                    "name": "provider",
                    "validate": _nonempty,
                    "invalid_message": "provider 不能为空",
                    "long_instruction": HINT_CANCEL,
                }
            ]
        )
        if manual is None:
            return None
        state["provider"] = manual["provider"].strip()
    else:
        state["provider"] = provider
    return 2


def step_models(state: dict, client: LiteLLMClient, config: dict):
    while True:
        models = state["models"]
        choices = [_separator()]
        if models:
            for m in models:
                choices.append(FuzzySeparator(m, indent=True, pinned=False))
        else:
            choices.append(FuzzySeparator("等待添加模型...", indent=True, pinned=False))
        choices.append(_separator())
        choices.append(Choice("add", name="添加模型"))
        choices.append(Choice("continue", name="继续"))
        choices.append(Choice("back", name="上一步"))
        result = ask(
            [
                {
                    "type": "fuzzy",
                    "message": f"添加模型（已添加 {len(models)} 个）:",
                    "name": "action",
                    "choices": choices,
                    "long_instruction": HINT_FUZZY,
                }
            ]
        )
        if result is None:
            return 1
        action = result["action"]
        if action in (None, "back"):
            return 1
        if action == "continue":
            if not models:
                print("请至少添加一个模型。")
                continue
            return 3
        if action == "add":
            model_filter(state, client, config)


def model_filter(state: dict, client: LiteLLMClient, config: dict):
    """筛选页：选择模型成功（非空）后返回模型列表页；取消（Ctrl+C/空）则停留在本页。"""
    while True:
        try:
            cost_map = get_cost_map(client)
        except KeyboardInterrupt:
            return
        built_in = cost_map_models(cost_map, state["provider"])
        bound = next(
            (
                u
                for u in (config.get("upstreams") or [])
                if u.get("provider") == state["provider"]
            ),
            None,
        )
        choices = []
        if built_in:
            choices.append(
                Choice("builtin", name=f"从 LiteLLM 内置模型列表获取")
            )
        upstream_label = "从配置的 Upstream 获取"
        if bound:
            upstream_label += f"（已绑定 {bound['name']}）"
        choices.append(Choice("upstream", name=upstream_label))
        choices.append(Choice("manual", name="手动输入"))
        choices.append(Choice("back", name="[返回]"))
        # 预选：绑定的 Upstream 优先（显式配置），其次内置列表可用，最后手动
        if bound:
            default_action = "upstream"
        elif built_in:
            default_action = "builtin"
        else:
            default_action = "manual"
        result = ask(
            [
                {
                    "type": "list",
                    "message": f"选择要从 {state['provider']} 添加的模型:",
                    "name": "action",
                    "choices": choices,
                    "default": default_action,
                    "long_instruction": HINT_LIST,
                }
            ]
        )
        if result is None:
            return
        action = result["action"]
        if action == "back":
            return
        if action == "builtin":
            picked = fuzzy_picker(built_in, f"选择 {state['provider']} 的内置模型:")
            if not picked:
                continue
            print(f"已添加 {append_models(state, picked)} 个模型。")
            return
        if action == "upstream":
            picked = upstream_picker(config, state.get("provider"))
            if not picked:
                continue
            print(f"已添加 {append_models(state, picked)} 个模型。")
            return
        if action == "manual":
            manual = ask(
                [
                    {
                        "type": "input",
                        "message": "请输入 LiteLLM Model Name:",
                        "name": "model",
                        "validate": _nonempty,
                        "invalid_message": "模型名称不能为空",
                        "long_instruction": HINT_CANCEL,
                    }
                ]
            )
            if manual is None:
                continue
            print(f"已添加 {append_models(state, [manual['model'].strip()])} 个模型。")
            return


def fuzzy_picker(models: list, message: str):
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": message,
                "name": "picked",
                "choices": [_separator()] + models + [_separator()],
                "multiselect": True,
                "keybindings": {"toggle": [{"key": "space"}]},
                "long_instruction": HINT_MULTI,
            }
        ]
    )
    if not result:
        return []
    return result["picked"]


def upstream_picker(config: dict, provider: str = None):
    upstreams = config.get("upstreams") or []
    if not upstreams:
        print("配置中没有 Upstream，无法使用此功能。可在配置文件的 upstreams 段添加。")
        return []
    bound = next(
        (u for u in upstreams if provider and u.get("provider") == provider), None
    )
    ordered = ([bound] + [u for u in upstreams if u is not bound]) if bound else list(upstreams)
    choices = [
        _separator(),
        *[
            Choice(
                u,
                name=f"{u['name']} ({u['type']})"
                + (" · 已绑定当前 provider" if u is bound else ""),
            )
            for u in ordered
        ],
        _separator(),
    ]
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": "请选择 Upstream:",
                "name": "upstream",
                "choices": choices,
                "long_instruction": HINT_FUZZY,
            }
        ]
    )
    if result is None:
        return []
    upstream = result["upstream"]
    if upstream is None:
        return []
    print(f"正在从 [{upstream['name']}] 拉取模型列表...")
    try:
        models = fetch_upstream_models(upstream)
    except KeyboardInterrupt:
        return []
    except UpstreamError as e:
        print(f"拉取失败: {e}")
        return []
    if not models:
        print("模型列表为空。")
        return []
    return fuzzy_picker(models, f"选择模型:")


def append_models(state: dict, new_models: list) -> int:
    existing = set(state["models"])
    added = 0
    for m in new_models:
        m = str(m).strip()
        if m and m not in existing:
            state["models"].append(m)
            existing.add(m)
            added += 1
    return added


def step_mappings(state: dict):
    focus = state.pop("focus", None)
    while True:
        models = state["models"]
        if focus in models:
            ordered = [focus] + [m for m in models if m != focus]
        else:
            ordered = models
            focus = None
        choices = [_separator()]
        for m in ordered:
            public = state["mappings"].get(m, m)
            choices.append(Choice(m, name=f"{public} <<< {m}"))
        choices.append(_separator())
        choices.append(Choice("continue", name="继续"))
        choices.append(Choice("back", name="上一步"))
        result = ask(
            [
                {
                    "type": "fuzzy",
                    "message": "Public Name 设置:",
                    "name": "action",
                    "choices": choices,
                    "long_instruction": HINT_FUZZY,
                }
            ]
        )
        if result is None:
            return 2
        action = result["action"]
        if action in (None, "back"):
            return 2
        if action == "continue":
            return 4
        answer = ask(
            [
                {
                    "type": "input",
                    "message": "请输入 Public Name:",
                    "name": "public",
                    "default": action,
                    "long_instruction": "留空则直接使用 LiteLLM Name · Ctrl+C 取消",
                }
            ]
        )
        if answer is None:
            continue
        state["mappings"][action] = answer["public"].strip() or action


def step_credential(state: dict, client: LiteLLMClient):
    credentials = fetch_credentials(client)
    choices = [_separator()]
    choices.extend(credential_rows(credentials))
    choices.append(_separator())
    choices.append(Choice(MANUAL_CHOICE, name="手动输入"))
    choices.append(Choice("back", name="上一步"))
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": "请选择 Credential:",
                "name": "credential",
                "choices": choices,
                "long_instruction": HINT_FUZZY,
            }
        ]
    )
    if result is None:
        return 3
    credential = result["credential"]
    if credential == "back":
        return 3
    if credential == MANUAL_CHOICE:
        manual = ask(
            [
                {
                    "type": "input",
                    "message": "请输入 Credential 名称（可留空表示不绑定）:",
                    "name": "credential",
                    "long_instruction": HINT_CANCEL,
                }
            ]
        )
        if manual is None:
            return 3
        state["credential"] = manual["credential"].strip()
        return 5
    state["credential"] = credential
    return 5


def step_review(state: dict, client: LiteLLMClient):
    print("\n========== 确认添加模型（选择对应行可直接编辑）==========")
    choices = [
        _separator(),
        Choice(1, name=f"Provider: {state['provider']}"),
        Choice(4, name=f"Credential: {state['credential'] or '无'}"),
        _separator(),
    ]
    for m in state["models"]:
        public = state["mappings"].get(m, m)
        choices.append(Choice(m, name=f"{public} <<< {m}"))
    choices.append(_separator())
    choices.append(Choice("submit", name="添加模型到 LiteLLM"))
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": "确认添加模型:",
                "name": "action",
                "choices": choices,
                "long_instruction": HINT_FUZZY,
            }
        ]
    )
    if result is None:
        return 4
    action = result["action"]
    if action == "submit":
        submit_models(state, client)
        return 6
    if action in (1, 4):
        return action
    if isinstance(action, str):
        # 点击了模型行：进入 Mappings 编辑并聚焦该模型
        state["focus"] = action
        return 3
    return 5


def submit_models(state: dict, client: LiteLLMClient):
    ok = 0
    failed = 0
    for m in state["models"]:
        public = state["mappings"].get(m, m)
        params = {"model": m, "custom_llm_provider": state["provider"]}
        if state["credential"]:
            params["litellm_credential_name"] = state["credential"]
        try:
            client.create_model(public, params)
            ok += 1
            print(f"  + 已添加: {public} ({m})")
        except LiteLLMError as e:
            failed += 1
            print(f"  x 失败: {public} -> {e}")
    print(f"\n完成: 成功 {ok} 个, 失败 {failed} 个。")
