"""路由管理屏：路由组列表与组详情。

路由组无专用 CRUD，统一“读-改-写”（load_routing_data → 修改 → update_router_settings）。
本模块只读模型数据，不修改任何模型设置。
"""
import asyncio

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Static

from ..client import LiteLLMError
from ..widgets import (
    STYLE_DIM,
    STYLE_ERR,
    STYLE_WARN,
    ConfirmModal,
    OutputModal,
    filter_fuzzy,
    load_rows,
    make_table,
)

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


# ---------------------------------------------------------------- 纯数据层

def load_routing_data(client) -> dict:
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


def fresh_groups(client) -> list:
    """写操作前重新读取最新 routing_groups，避免覆盖他人改动。"""
    router = client.get_router_settings()
    return (router.get("current_values") or {}).get("routing_groups") or []


def group_of(groups: list, name: str):
    return next((g for g in groups if g.get("group_name") == name), None)


def group_validity(group: dict, models_by_name: dict):
    """组内模型校验：返回 (有效, 失效)。"""
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


# ---------------------------------------------------------------- 列表页

class RoutingListScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self):
        super().__init__()
        self._data: dict = {"groups": [], "strategies": [], "models_by_name": {}, "global_strategy": ""}
        self._display: list = []
        self._filter = ""

    def compose(self):
        yield Static(Text("路由管理", style="bold"), classes="page-title", id="rt-title")
        with Horizontal(classes="search-row"):
            yield Static("搜索  ", classes="page-hint")
            yield Input(placeholder="按组名筛选（/ 聚焦）", id="rt-search")
        with Horizontal(classes="toolbar"):
            yield Button("＋ 添加路由组", id="add", variant="primary")
            yield Button("清除失效模型", id="clean")
            yield Button("刷新", id="refresh")
            yield Button("返回", id="back")
        table = make_table("组名", "路由策略", "模型数", "状态", cursor="row")
        table.id = "rt-table"
        yield table
        yield Static("↑↓ 移动 · 回车 查看详情 · Esc 返回", classes="page-hint")

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    @work(exclusive=True)
    async def _load(self) -> None:
        client = self.app.get_client()
        self.app.status("正在获取路由组…")
        try:
            data = await asyncio.to_thread(load_routing_data, client)
        except LiteLLMError as e:
            self.app.notify_err(f"获取路由组失败: {e}")
            data = {"groups": [], "strategies": [], "models_by_name": {}, "global_strategy": ""}
        self._data = data
        self._rebuild()
        self.app.status(f"共 {len(data['groups'])} 个路由组")

    def _rebuild(self) -> None:
        groups = sorted(self._data["groups"], key=lambda g: g.get("group_name") or "")
        if self._filter:
            groups = filter_fuzzy(groups, self._filter, lambda g: g.get("group_name", ""))
        mbn = self._data["models_by_name"]
        rows = []
        for g in groups:
            valid, invalid = group_validity(g, mbn)
            total = len(g.get("models") or [])
            status = (
                Text(f"⚠ {len(invalid)} 个失效", style=STYLE_WARN)
                if invalid else Text("正常", style="dim")
            )
            strat = g.get("routing_strategy") or "-"
            rows.append([
                Text(g.get("group_name") or "?"),
                Text(f"{strat} · {strategy_label(strat)}" if strategy_label(strat) else strat),
                Text(f"{len(valid)}/{total}"),
                status,
            ])
        self._display = groups
        load_rows(self.query_one("#rt-table", DataTable), rows)
        self.query_one("#rt-title", Static).update(
            Text(f"路由管理（{len(groups)}/{len(self._data['groups'])}）", style="bold")
        )

    @on(Input.Changed, "#rt-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(DataTable.RowSelected, "#rt-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._display):
            g = self._display[event.cursor_row]
            self.app.push_screen(GroupDetailScreen(g.get("group_name")))

    @on(Button.Pressed, "#add")
    def _on_add(self) -> None:
        self._open_wizard(None)

    def _open_wizard(self, group) -> None:
        from .routing_wizard import GroupWizardScreen

        async def done(ok: bool) -> None:
            if ok:
                self._load()

        self.app.push_screen(GroupWizardScreen(group), done)

    @on(Button.Pressed, "#clean")
    def _on_clean(self) -> None:
        self._clean()

    @on(Button.Pressed, "#refresh")
    def _on_refresh(self) -> None:
        self._load()

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_focus_search(self) -> None:
        self.query_one("#rt-search", Input).focus()

    def _clean(self) -> None:
        data = self._data
        affected = []
        for g in data["groups"]:
            invalid = [m for m in g.get("models") or [] if m not in data["models_by_name"]]
            if invalid:
                affected.append((g, invalid))
        if not affected:
            self.app.notify_ok("未发现失效模型")
            return
        lines = Text()
        for g, invalid in affected:
            lines.append(f"{g.get('group_name')}: {', '.join(invalid)}\n", style=STYLE_WARN)
        emptied = [
            g.get("group_name") for g, inv in affected
            if len(g.get("models") or []) == len(inv)
        ]
        if emptied:
            lines.append(f"注意: 清除后以下组将无剩余模型，将整体移除: {', '.join(emptied)}\n", style=STYLE_DIM)
        total_inv = sum(len(i) for _, i in affected)
        self.app.push_screen(
            ConfirmModal(
                f"{lines}\n确认从 {len(affected)} 个路由组中移除以上 {total_inv} 个失效模型？",
                title="清除失效模型", default_yes=False, yes="确认清除",
            ),
            lambda ok: self._do_clean() if ok else None,
        )

    @work(exclusive=True)
    async def _do_clean(self) -> None:
        client = self.app.get_client()

        def do() -> int:
            data = load_routing_data(client)  # 以最新数据为准写回
            new_groups = []
            removed = 0
            for g in data["groups"]:
                models = [m for m in g.get("models") or [] if m in data["models_by_name"]]
                removed += len(g.get("models") or []) - len(models)
                if models:
                    g = dict(g)
                    g["models"] = models
                    new_groups.append(g)
            client.update_router_settings({"routing_groups": new_groups})
            return removed

        self.app.status("正在清除失效模型…")
        try:
            removed = await asyncio.to_thread(do)
            self.app.notify_ok(f"完成: 移除 {removed} 个失效模型")
            self._load()
        except LiteLLMError as e:
            self.app.notify_err(f"清除失败: {e}")


# ---------------------------------------------------------------- 详情页

class GroupDetailScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self, group_name: str):
        super().__init__()
        self.group_name = group_name
        self._group: dict | None = None
        self._member_names: list = []

    def compose(self):
        yield Static(Text("路由组详情", style="bold"), classes="page-title", id="gd-title")
        yield Static("", id="gd-strategy", classes="page-hint")
        with Horizontal(classes="toolbar"):
            yield Button("编辑路由组", id="edit", variant="primary")
            yield Button("删除路由组", id="delete")
            yield Button("刷新", id="refresh")
            yield Button("返回列表", id="back")
        table = make_table("模型名", "Provider", "Credential", "tpm/rpm", "状态", cursor="row")
        table.id = "gd-table"
        yield table
        yield Static("↑↓ 移动 · 回车 查看模型详情 · r 刷新 · Esc 返回列表", classes="page-hint")

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    @work(exclusive=True)
    async def _load(self) -> None:
        client = self.app.get_client()
        self.app.status("正在获取路由组…")
        try:
            data = await asyncio.to_thread(load_routing_data, client)
        except LiteLLMError as e:
            self.app.notify_err(f"获取路由组失败: {e}")
            return
        g = group_of(data["groups"], self.group_name)
        if g is None:
            self.app.notify_warn("路由组已不存在，返回列表")
            self.app.pop_screen()
            return
        self._group = g
        mbn = data["models_by_name"]
        valid, invalid = group_validity(g, mbn)
        rows = []
        names = []
        for name, m in valid:
            params = (m.get("litellm_params") or {})
            info = (m.get("model_info") or {})
            tpm, rpm = info.get("tpm"), info.get("rpm")
            tr = f"{tpm}/{rpm}" if (tpm is not None or rpm is not None) else "-"
            if info.get("blocked"):
                status = Text("🔴 已禁用", style=STYLE_ERR)
            else:
                status = Text("🟢 已启用", style="#4ec96a")
            names.append(name)
            rows.append([
                Text(name),
                Text(params.get("custom_llm_provider") or "-"),
                Text(params.get("litellm_credential_name") or "-"),
                Text(tr),
                status,
            ])
        for name in invalid:
            names.append(name)
            rows.append([
                Text(name, style=STYLE_DIM),
                Text("-", style=STYLE_DIM),
                Text("-", style=STYLE_DIM),
                Text("-", style=STYLE_DIM),
                Text("⚠ 失效", style=STYLE_WARN),
            ])
        self._member_names = names
        load_rows(self.query_one("#gd-table", DataTable), rows)
        strat = g.get("routing_strategy") or "-"
        self.query_one("#gd-title", Static).update(
            Text(f"路由组: {self.group_name}（{len(valid)} 有效 / {len(invalid)} 失效）", style="bold")
        )
        self.query_one("#gd-strategy", Static).update(
            f"路由策略: {strat}（{strategy_label(strat) or '?'}）  ·  全局默认: {data['global_strategy']}"
        )
        self._data_models = mbn

    @on(DataTable.RowSelected, "#gd-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if not (0 <= event.cursor_row < len(self._member_names)):
            return
        name = self._member_names[event.cursor_row]
        m = getattr(self, "_data_models", {}).get(name)
        if m is None:
            self.app.push_screen(OutputModal(f"模型: {name}", Text(f"模型 {name} 在模型列表中不存在（失效成员）。", style=STYLE_WARN)))
            return
        params = m.get("litellm_params") or {}
        info = m.get("model_info") or {}
        status = "🔴 已禁用" if info.get("blocked") else "🟢 启用中"
        content = Text()
        content.append(f"模型: {m.get('model_name', '?')}\n", style="bold")
        content.append(f"provider: {params.get('custom_llm_provider', '?')}\n")
        content.append(f"credential: {params.get('litellm_credential_name', '无')}\n")
        from ..modeldata import cost_text
        content.append(f"cost: {cost_text(info)} $/1M tokens\n")
        content.append(f"tpm: {info.get('tpm', '-')}  rpm: {info.get('rpm', '-')}  状态: {status}\n")
        self.app.push_screen(OutputModal(f"模型: {name}", content))

    @on(Button.Pressed, "#edit")
    def _on_edit(self) -> None:
        if self._group is None:
            return
        from .routing_wizard import GroupWizardScreen

        async def done(ok: bool) -> None:
            if ok:
                self._load()

        self.app.push_screen(GroupWizardScreen(self._group), done)

    @on(Button.Pressed, "#delete")
    def _on_delete(self) -> None:
        g = self._group
        if g is None:
            return
        total = len(g.get("models") or [])
        self.app.push_screen(
            ConfirmModal(
                f"确认删除路由组 {self.group_name}？\n"
                f"  · 仅移除路由组配置，组内 {total} 个成员模型本身不会删除\n"
                f"  · 删除后成员模型回落到全局路由策略",
                title="删除路由组", default_yes=False, yes="确认删除",
            ),
            lambda ok: self._do_delete() if ok else None,
        )

    @work(exclusive=True)
    async def _do_delete(self) -> None:
        client = self.app.get_client()
        name = self.group_name

        def do() -> bool:
            groups = fresh_groups(client)
            new_groups = [g for g in groups if g.get("group_name") != name]
            if len(new_groups) == len(groups):
                return True  # 已不存在
            client.update_router_settings({"routing_groups": new_groups})
            return True

        try:
            await asyncio.to_thread(do)
            self.app.notify_ok(f"路由组 {self.group_name} 已删除")
            self.app.pop_screen()
        except LiteLLMError as e:
            self.app.notify_err(f"删除失败: {e}")

    @on(Button.Pressed, "#refresh")
    def _on_refresh(self) -> None:
        self._load()

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()
