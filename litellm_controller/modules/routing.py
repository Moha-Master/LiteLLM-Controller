"""路由管理模块：路由组列表/详情、添加/编辑向导、删除路由组、清除失效模型。

路由组 = router_settings.routing_groups 条目（group_name / models / routing_strategy），
无专用 CRUD 端点，统一走"读-改-写"：GET /router/settings 取全量 → 修改 →
POST /config/update 写回（routing_groups 整表提交，立即生效）。

约束（LiteLLM 校验）：
- 一个模型（model_name）最多属于一个路由组，重复归属会导致路由组整体失效；
- group_name 不能为保留字 default，不能与现有路由组重名；
- group_name 与现有模型名冲突时 LiteLLM 仅警告（调用可能产生歧义），本模块提示但不阻止。

整个模块只读模型数据，不修改任何模型相关设置。

交互约定：
- 操作提示统一放在各提示底部的 long_instruction 行，提示文本中不重复。
- 数据行在上，操作选项（含返回）统一放在底部，中间以分隔线隔开。
- Ctrl+C 返回上一级，各层级同时提供"返回/上一步"选项。
"""
from InquirerPy.base.control import Choice

from .. import ui  # noqa: F401  导入即注册 DotFuzzy
from ..client import LiteLLMClient, LiteLLMError
from ..config import load_config
from ..modules.models import (
    HINT_CANCEL,
    HINT_FUZZY,
    HINT_LIST,
    HINT_MULTI,
    _clip,
    _current_first,
    _separator,
    ask,
)
from ..ui import DisabledChoice, FuzzySeparator

DEFAULT_STRATEGIES = [
    "simple-shuffle",
    "least-busy",
    "latency-based-routing",
    "cost-based-routing",
    "usage-based-routing",
    "usage-based-routing-v2",
    "lar1",
]

STRATEGY_LABELS = {
    "simple-shuffle": "随机分发（默认）",
    "least-busy": "最少进行中请求",
    "latency-based-routing": "滑动窗口最低延迟",
    "cost-based-routing": "最低 token 成本",
    "usage-based-routing": "最低 TPM 用量（已弃用）",
    "usage-based-routing-v2": "最低 TPM 用量（v2）",
    "lar1": "LAR-1（实验性）",
    "provider-budget-routing": "provider 预算限制",
}


def strategy_label(s: str) -> str:
    return STRATEGY_LABELS.get(s, "")


# ---------------------------------------------------------------- 基础数据


def load_routing_data(client: LiteLLMClient) -> dict:
    """获取路由组、策略选项与模型列表（model_name → 模型条目）。"""
    router = client.get_router_settings()
    current = router.get("current_values") or {}
    strategies = None
    for f in router.get("fields") or []:
        if f.get("field_name") == "routing_strategy" and f.get("options"):
            strategies = list(f["options"])
            break
    if not strategies:
        strategies = list(DEFAULT_STRATEGIES)
    models = client.list_models()
    models_by_name = {}
    for m in models:
        name = m.get("model_name")
        if name:
            models_by_name.setdefault(name, m)
    return {
        "groups": current.get("routing_groups") or [],
        "strategies": strategies,
        "global_strategy": current.get("routing_strategy") or "simple-shuffle",
        "models_by_name": models_by_name,
    }


def _fresh_groups(client: LiteLLMClient) -> list:
    """写操作前重新读取最新 routing_groups，避免覆盖他人改动。"""
    router = client.get_router_settings()
    return (router.get("current_values") or {}).get("routing_groups") or []


def group_of(groups: list, name: str):
    return next((g for g in groups if g.get("group_name") == name), None)


def group_validity(group: dict, models_by_name: dict):
    """组内模型校验：返回 (有效, 失效)。有效为 [(model_name, 模型条目), ...]，失效为 [model_name, ...]。"""
    valid, invalid = [], []
    for name in group.get("models") or []:
        m = models_by_name.get(name)
        if m:
            valid.append((name, m))
        else:
            invalid.append(name)
    return valid, invalid


def model_membership(groups: list) -> dict:
    """model_name → 所属路由组名。"""
    membership = {}
    for g in groups:
        for m in g.get("models") or []:
            membership.setdefault(m, g.get("group_name", "?"))
    return membership


# ---------------------------------------------------------------- 行渲染


def _row_prov(row):
    m = row[1]
    if not m:
        return "-"
    return (m.get("litellm_params") or {}).get("custom_llm_provider") or "-"


def _row_cred(row):
    m = row[1]
    if not m:
        return "-"
    return (m.get("litellm_params") or {}).get("litellm_credential_name") or "-"


def _member_widths(rows: list) -> dict:
    def width(getter, cap, default):
        vals = [len(getter(r)) for r in rows]
        return min(max(vals + [default]), cap) if vals else default

    return {
        "name": width(lambda r: r[0], 30, 10),
        "prov": width(_row_prov, 14, 8),
        "cred": width(_row_cred, 16, 8),
        "tr": 14,
    }


def _member_row(name, m, widths) -> str:
    params = (m.get("litellm_params") or {}) if m else {}
    info = (m.get("model_info") or {}) if m else {}
    prov = _clip(params.get("custom_llm_provider") or "-", widths["prov"])
    cred = _clip(params.get("litellm_credential_name") or "-", widths["cred"])
    tpm, rpm = info.get("tpm"), info.get("rpm")
    tr = f"{tpm}/{rpm}" if (tpm is not None or rpm is not None) else "-"
    if m is None:
        status = "⚠ 失效"
    elif info.get("blocked"):
        status = "🔴 已禁用"
    else:
        status = "🟢 已启用"
    return (
        f"{_clip(name, widths['name']):<{widths['name']}} {prov:<{widths['prov']}} "
        f"{cred:<{widths['cred']}} {tr:<{widths['tr']}} {status}"
    )


def _member_header(widths) -> str:
    return (
        f"{'模型名':<{widths['name']}} {'Provider':<{widths['prov']}} "
        f"{'Credential':<{widths['cred']}} {'tpm/rpm':<{widths['tr']}} 状态"
    )


def _group_widths(groups: list) -> dict:
    name_w = min(max([len(g.get("group_name") or "") for g in groups] + [4]), 26)
    strat_w = min(max([len(g.get("routing_strategy") or "") for g in groups] + [4]), 24)
    return {"name": name_w, "strat": strat_w}


def _group_row(g: dict, widths: dict, models_by_name: dict) -> str:
    valid, invalid = group_validity(g, models_by_name)
    total = len(g.get("models") or [])
    return (
        f"{(g.get('group_name') or '?'):<{widths['name']}} "
        f"{(g.get('routing_strategy') or '-'):<{widths['strat']}} {len(valid)}/{total}"
        + (f"  ⚠{len(invalid)} 失效" if invalid else "")
    )


# ---------------------------------------------------------------- 路由组列表页


def routing_module():
    config = load_config()
    client = LiteLLMClient(config["litellm"]["endpoint"], config["litellm"]["key"])
    while True:
        print("\n正在获取路由组...")
        try:
            data = load_routing_data(client)
        except KeyboardInterrupt:
            return
        except LiteLLMError as e:
            print(f"获取路由组失败: {e}")
            return
        groups = sorted(data["groups"], key=lambda g: g.get("group_name") or "")
        choices = [_separator()]
        if groups:
            widths = _group_widths(groups)
            choices.append(
                FuzzySeparator(
                    f"{'组名':<{widths['name']}} {'路由策略':<{widths['strat']}} 模型数",
                    indent=True,
                )
            )
            for g in groups:
                choices.append(Choice(g["group_name"], name=_group_row(g, widths, data["models_by_name"])))
        else:
            choices.append(FuzzySeparator("（暂无路由组）", indent=True, pinned=False))
        choices.append(_separator())
        choices.append(Choice("add", name="添加路由组"))
        choices.append(Choice("clean", name="清除路由组中的失效模型"))
        choices.append(Choice("refresh", name="刷新路由组"))
        choices.append(Choice(None, name="[返回主菜单]"))
        result = ask(
            [
                {
                    "type": "fuzzy",
                    "message": f"当前有 {len(groups)} 个路由组:",
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
        if selection == "add":
            add_group_wizard(client)
        elif selection == "clean":
            clean_invalid_models(client)
        elif selection == "refresh":
            continue
        else:
            group = group_of(data["groups"], selection)
            if group is not None:
                show_group(client, group)


# ---------------------------------------------------------------- 路由组详情


def show_group(client: LiteLLMClient, group: dict):
    while True:
        try:
            data = load_routing_data(client)
        except KeyboardInterrupt:
            return
        except LiteLLMError as e:
            print(f"获取路由组失败: {e}")
            return
        g = group_of(data["groups"], group.get("group_name"))
        if g is None:
            print("路由组已不存在，返回列表。")
            return
        group = g
        models_by_name = data["models_by_name"]
        valid, invalid = group_validity(group, models_by_name)
        rows = valid + [(name, None) for name in invalid]
        widths = _member_widths(rows)
        choices = [
            _separator(),
            FuzzySeparator(_member_header(widths), indent=True),
        ]
        for name, m in rows:
            row = _member_row(name, m, widths)
            if m is None:
                disabled = DisabledChoice(name, reason="模型不存在")
                choices.append(Choice(disabled, name=f"{row}  → 模型列表中不存在"))
            else:
                choices.append(Choice(name, name=row))
        if not rows:
            choices.append(FuzzySeparator("（组内无模型）", indent=True, pinned=False))
        choices.append(_separator())
        choices.append(Choice("edit", name="编辑路由组"))
        choices.append(Choice("delete", name="删除路由组"))
        choices.append(Choice(None, name="[返回路由组列表]"))
        name = group.get("group_name", "?")
        strategy = group.get("routing_strategy") or "-"
        result = ask(
            [
                {
                    "type": "fuzzy",
                    "message": (
                        f"路由组: {name}  |  策略: {strategy}（{strategy_label(strategy) or '?'}）  |  "
                        f"模型: {len(valid)} 有效 / {len(invalid)} 失效"
                    ),
                    "name": "action",
                    "choices": choices,
                    "long_instruction": HINT_FUZZY,
                }
            ]
        )
        if result is None:
            return
        action = result["action"]
        if action is None:
            return
        if action == "edit":
            new_name = edit_group_wizard(client, group)
            if new_name:
                group = {"group_name": new_name}
            continue
        if action == "delete":
            if delete_group(client, group, data):
                return
            continue
        member = models_by_name.get(action)
        if member is not None:
            member_info(member)


def member_info(member: dict):
    params = member.get("litellm_params") or {}
    info = member.get("model_info") or {}

    def _c(v):
        return "-" if v is None else f"{v * 1e6:.4g}"

    status = "🔴 已禁用" if info.get("blocked") else "🟢 启用中"
    ask(
        [
            {
                "type": "list",
                "message": (
                    f"模型: {member.get('model_name', '?')}\n"
                    f"  provider: {params.get('custom_llm_provider', '?')}  "
                    f"credential: {params.get('litellm_credential_name', '无')}  "
                    f"cost: {_c(info.get('input_cost_per_token'))}/{_c(info.get('output_cost_per_token'))} $/1M tokens\n"
                    f"  tpm: {info.get('tpm', '-')}  rpm: {info.get('rpm', '-')}  状态: {status}"
                ),
                "name": "back",
                "choices": [Choice(None, name="[返回]")],
                "long_instruction": HINT_LIST,
            }
        ]
    )


def delete_group(client: LiteLLMClient, group: dict, data: dict) -> bool:
    name = group.get("group_name", "?")
    total = len(group.get("models") or [])
    global_strategy = data.get("global_strategy", "simple-shuffle")
    confirm = ask(
        [
            {
                "type": "confirm",
                "message": (
                    f"确认删除路由组 {name}？\n"
                    f"  - 仅移除路由组配置，组内 {total} 个成员模型本身不会删除\n"
                    f"  - 删除后成员模型回落到全局路由策略（{global_strategy}）"
                ),
                "name": "ok",
                "default": False,
            }
        ]
    )
    if not confirm or not confirm.get("ok"):
        return False
    try:
        groups = _fresh_groups(client)
        new_groups = [g for g in groups if g.get("group_name") != name]
        if len(new_groups) == len(groups):
            print("路由组已不存在，无需删除。")
            return True
        client.update_router_settings({"routing_groups": new_groups})
        print(f"路由组 {name} 已删除。")
        return True
    except LiteLLMError as e:
        print(f"删除失败: {e}")
        return False


def clean_invalid_models(client: LiteLLMClient):
    print("正在扫描失效模型...")
    try:
        data = load_routing_data(client)
    except KeyboardInterrupt:
        return
    except LiteLLMError as e:
        print(f"获取路由组失败: {e}")
        return
    affected = []
    for g in data["groups"]:
        invalid = [m for m in g.get("models") or [] if m not in data["models_by_name"]]
        if invalid:
            affected.append((g, invalid))
    if not affected:
        print("未发现失效模型。")
        return
    for g, invalid in affected:
        print(f"  {g.get('group_name')}: {', '.join(invalid)}")
    emptied = [
        g.get("group_name")
        for g, inv in affected
        if len(g.get("models") or []) == len(inv)
    ]
    if emptied:
        print(f"注意: 清除后以下组将无剩余模型，将整体移除: {', '.join(emptied)}")
    confirm = ask(
        [
            {
                "type": "confirm",
                "message": f"确认从 {len(affected)} 个路由组中移除以上 {sum(len(i) for _, i in affected)} 个失效模型？",
                "name": "ok",
                "default": False,
            }
        ]
    )
    if not confirm or not confirm.get("ok"):
        print("已取消。")
        return
    try:
        # 以最新数据为准写回
        data2 = load_routing_data(client)
        new_groups = []
        removed = 0
        for g in data2["groups"]:
            models = [m for m in g.get("models") or [] if m in data2["models_by_name"]]
            removed += len(g.get("models") or []) - len(models)
            if models:
                g = dict(g)
                g["models"] = models
                new_groups.append(g)
        client.update_router_settings({"routing_groups": new_groups})
        print(f"完成: 移除 {removed} 个失效模型。")
    except KeyboardInterrupt:
        return
    except LiteLLMError as e:
        print(f"清除失败: {e}")


# ---------------------------------------------------------------- 添加/编辑向导
# 步骤: 1 名称 → 2 模型列表 → 3 路由策略 → 4 确认
# 统一操作逻辑: 从确认页跳转进入的步骤，完成后一律回到确认页。


def add_group_wizard(client: LiteLLMClient):
    state = {
        "orig_name": None,
        "orig_models": [],
        "orig_strategy": None,
        "name": None,
        "models": [],
        "strategy": None,
    }
    step = 1
    jump_back_to_review = False
    while True:
        data = _wizard_data(client)
        if data is None:
            return
        if step == 4:
            nxt = step_group_review(state, client)
            if nxt is None:
                return
            if nxt >= 5:
                return
            if nxt == 4:
                continue
            step = nxt
            jump_back_to_review = True
            continue
        if step == 1:
            nxt = step_group_name(state, data)
        elif step == 2:
            nxt = step_group_models(state, data)
        else:
            nxt = step_group_strategy(state, data)
        if nxt is None:
            return
        if jump_back_to_review:
            nxt = 4
            jump_back_to_review = False
        step = nxt


def edit_group_wizard(client: LiteLLMClient, group: dict):
    """返回最终组名；取消返回 None。"""
    state = {
        "orig_name": group.get("group_name"),
        "orig_models": list(group.get("models") or []),
        "orig_strategy": group.get("routing_strategy"),
        "name": group.get("group_name"),
        "models": list(group.get("models") or []),
        "strategy": group.get("routing_strategy") or "simple-shuffle",
    }
    step = 1
    jump_back_to_review = False
    while True:
        data = _wizard_data(client)
        if data is None:
            return None
        if step == 4:
            nxt = step_group_review(state, client)
            if nxt is None:
                return None
            if nxt >= 5:
                return state["name"]
            if nxt == 4:
                continue
            step = nxt
            jump_back_to_review = True
            continue
        if step == 1:
            nxt = step_group_name(state, data, state["orig_name"])
        elif step == 2:
            nxt = step_group_models(state, data, state["orig_name"])
        else:
            nxt = step_group_strategy(state, data)
        if nxt is None:
            return None
        if jump_back_to_review:
            nxt = 4
            jump_back_to_review = False
        step = nxt


def _wizard_data(client: LiteLLMClient):
    try:
        return load_routing_data(client)
    except KeyboardInterrupt:
        return None
    except LiteLLMError as e:
        print(f"获取数据失败: {e}")
        return None


def _validate_group_name(other_names: set):
    def validate(val) -> bool:
        v = (val or "").strip()
        if not v or v == "default" or v in other_names:
            return False
        return True

    return validate


def step_group_name(state: dict, data: dict, orig_name: str = None):
    if orig_name is None:
        other = {g.get("group_name") for g in data["groups"]}
        message = "请输入路由组名称:"
        default = ""
    else:
        other = {
            g.get("group_name") for g in data["groups"] if g.get("group_name") != orig_name
        }
        message = "路由组名称（修改）:"
        default = orig_name
    result = ask(
        [
            {
                "type": "input",
                "message": message,
                "name": "name",
                "default": default,
                "validate": _validate_group_name(other),
                "invalid_message": "名称不能为空，default 为保留字，且不能与现有路由组重名",
                "long_instruction": HINT_CANCEL,
            }
        ]
    )
    if result is None:
        return None
    name = result["name"].strip()
    state["name"] = name
    if name in data["models_by_name"]:
        print(f"注意: '{name}' 已是模型名，可作为路由组名但调用该模型将会产生歧义。")
    return 2


def step_group_models(state: dict, data: dict, orig_name: str = None):
    models_by_name = data["models_by_name"]
    membership = model_membership(data["groups"])
    current = set(state["models"])
    rows = []
    all_rows = [(name, models_by_name[name]) for name in sorted(models_by_name)]
    widths = _member_widths(all_rows)
    for name, m in all_rows:
        row = _member_row(name, m, widths)
        owner = membership.get(name)
        if owner and owner != orig_name:
            disabled = DisabledChoice(name, reason=f"已属于 {owner}")
            rows.append(Choice(disabled, name=f"{row}  → {owner}"))
        else:
            rows.append(Choice(name, name=row, enabled=name in current))
    rows.sort(key=lambda c: 0 if c.enabled else 1)
    if not rows:
        print("模型列表为空，无法选择模型。")
        if orig_name is None:
            return None
        result = ask(
            [
                {
                    "type": "list",
                    "message": "模型列表为空，将保持当前模型选择。",
                    "name": "action",
                    "choices": [
                        Choice("continue", name="继续"),
                        Choice("back", name="上一步"),
                    ],
                    "long_instruction": HINT_LIST,
                }
            ]
        )
        if result is None:
            return 1
        return 3 if result["action"] == "continue" else 1
    choices = [_separator()] + rows + [_separator()]
    # 多选模式：回车即确认选择（无"继续"行），Ctrl+C 返回上一步
    message = "请选择模型（空格勾选）:"
    if orig_name is not None:
        message = f"请选择模型（空格勾选，当前已选 {len(current)} 个）:"
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": message,
                "name": "models",
                "choices": choices,
                "multiselect": True,
                "keybindings": {"toggle": [{"key": "space"}]},
                "long_instruction": HINT_MULTI,
            }
        ]
    )
    if result is None:
        return 1
    picked = result["models"] or []
    state["models"] = [m for m in picked if isinstance(m, str)]
    if not state["models"]:
        print("请至少选择一个模型。")
        return 2
    return 3


def step_group_strategy(state: dict, data: dict):
    strategies = data["strategies"]
    choices = [
        Choice(s, name=f"{s:<24} {strategy_label(s)}") for s in strategies
    ]
    if state["strategy"] in strategies:
        choices = _current_first(state["strategy"], choices, marker="（当前）")
    choices = [_separator()] + choices + [_separator(), Choice("back", name="上一步")]
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": "请选择路由策略:",
                "name": "strategy",
                "choices": choices,
                "long_instruction": HINT_FUZZY,
            }
        ]
    )
    if result is None:
        return 2
    strategy = result["strategy"]
    if strategy == "back":
        return 2
    state["strategy"] = strategy
    return 4


def step_group_review(state: dict, client: LiteLLMClient):
    editing = state.get("orig_name") is not None
    added = (
        [m for m in state["models"] if m not in state["orig_models"]]
        if editing
        else list(state["models"])
    )
    removed = [m for m in state["orig_models"] if m not in state["models"]] if editing else []
    added_set = set(added)
    print("\n========== 确认路由组（选择对应行可直接编辑）==========")
    choices = [_separator()]
    name_label = f"名称: {state['name']}"
    if editing and state["name"] != state["orig_name"]:
        name_label += f"  (原: {state['orig_name']})"
    choices.append(Choice(1, name=name_label))
    strat_label = f"路由策略: {state['strategy']}"
    if editing and state["strategy"] != state["orig_strategy"]:
        strat_label += f"  (原: {state['orig_strategy']})"
    choices.append(Choice(3, name=strat_label))
    choices.append(_separator())
    if editing:
        choices.append(
            FuzzySeparator(
                f"模型: {len(state['orig_models'])} → {len(state['models'])}（新增 {len(added)} · 移除 {len(removed)}）",
                indent=True,
                pinned=False,
            )
        )
    for m in state["models"]:
        mark = "+" if (editing and m in added_set) else " "
        choices.append(Choice(m, name=f"{mark} {m}"))
    for m in removed:
        choices.append(Choice(DisabledChoice(m, reason="已移除"), name=f"- {m}（已移除）"))
    choices.append(_separator())
    choices.append(Choice("submit", name="应用修改" if editing else "添加路由组"))
    result = ask(
        [
            {
                "type": "fuzzy",
                "message": f"确认路由组（共 {len(state['models'])} 个模型）:",
                "name": "action",
                "choices": choices,
                "long_instruction": HINT_FUZZY,
            }
        ]
    )
    if result is None:
        return 3
    action = result["action"]
    if action == "submit":
        submit_group(state, client)
        return 5
    if action in (1, 3):
        return action
    if isinstance(action, str):
        return 2
    return 4


def submit_group(state: dict, client: LiteLLMClient) -> bool:
    name = state["name"]
    try:
        groups = _fresh_groups(client)
        others = [g for g in groups if g.get("group_name") != state.get("orig_name")]
        if any(g.get("group_name") == name for g in others):
            print(f"写入失败: 路由组 {name} 已存在。")
            return False
        dup = set()
        for g in others:
            dup |= set(g.get("models") or []) & set(state["models"])
        if dup:
            owners = sorted(
                {
                    g.get("group_name")
                    for g in others
                    if set(g.get("models") or []) & set(state["models"])
                }
            )
            print(f"写入失败: {', '.join(sorted(dup))} 已属于其他路由组（{', '.join(owners)}）。")
            return False
        entry = {
            "group_name": name,
            "models": list(state["models"]),
            "routing_strategy": state["strategy"],
        }
        if state.get("orig_name"):
            orig = group_of(groups, state["orig_name"])
            if orig is not None and orig.get("routing_strategy_args"):
                entry["routing_strategy_args"] = orig["routing_strategy_args"]
        new_groups = []
        replaced = False
        for g in groups:
            if state.get("orig_name") and g.get("group_name") == state["orig_name"]:
                new_groups.append(entry)
                replaced = True
            else:
                new_groups.append(g)
        if not replaced:
            new_groups.append(entry)
        client.update_router_settings({"routing_groups": new_groups})
        verb = "已更新" if state.get("orig_name") else "已添加"
        print(f"路由组{verb}: {name}（{len(state['models'])} 个模型, {state['strategy']}）")
        return True
    except LiteLLMError as e:
        print(f"写入失败: {e}")
        return False
