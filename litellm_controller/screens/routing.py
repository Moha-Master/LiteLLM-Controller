"""路由管理屏：路由组列表与组详情。

路由组无专用 CRUD，统一“读-改-写”（load_routing_data → 修改 → update_router_settings）。
本模块只读模型数据，不修改任何模型设置。
"""
import asyncio

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static

from ..client import LiteLLMError
from ..ui import PageScreen
from ..widgets import (
    STYLE_DIM,
    STYLE_OK,
    STYLE_WARN,
    ConfirmModal,
    filter_fuzzy,
    fit_table_columns,
    load_rows,
    make_table,
    rcell,
    shorten,
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


# ---------------------------------------------------------------- 列表页

class RoutingListScreen(PageScreen):
    """路由组列表：单击 / 回车进入组详情。"""

    TITLE = "路由管理"
    HINT = "↑↓ 移动 · 单击/回车 查看详情 · Ctrl+N 新建 · / 搜索 · Ctrl+R 刷新 · Esc/Ctrl+C 返回"

    BINDINGS = [
        Binding("/", "focus_search", "搜索", show=False),
        Binding("ctrl+n", "new_group", "新建", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._data: dict = {"groups": [], "strategies": [], "models_by_name": {}, "global_strategy": ""}
        self._display: list = []
        self._filter = ""

    def compose_page(self):
        with Vertical(classes="panel"):
            with Horizontal(classes="filter-row"):
                yield Static("搜索", classes="fl-label")
                yield Input(placeholder="按组名筛选（/ 聚焦）", compact=True, id="rt-search")
            with Horizontal(classes="filter-row gap-top"):
                yield Button("＋ 添加路由组", id="add", variant="primary")
                yield Static(classes="fill")
                yield Button("清除失效模型", id="clean")
        table = make_table("组名", "路由策略", "模型数", "状态")
        table.id = "rt-table"
        yield table

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    # ------------------------------------------------------------ 数据加载

    def reload_page(self) -> None:
        self._load()

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

    def _rebuild(self, *, after_layout: bool = False) -> None:
        groups = sorted(self._data["groups"], key=lambda g: g.get("group_name") or "")
        if self._filter:
            groups = filter_fuzzy(groups, self._filter, lambda g: g.get("group_name", ""))
        self._display = groups
        mbn = self._data["models_by_name"]
        table = self.query_one("#rt-table", DataTable)
        vis = fit_table_columns(table, [18, 40, 8, 12], rows=len(groups))
        keys = list(table.columns.keys())
        table.columns[keys[2]].label = rcell("模型数", vis[2] - 2)
        rows = []
        for g in groups:
            valid, invalid = group_validity(g, mbn)
            total = len(g.get("models") or [])
            status = rcell(f"⚠ {len(invalid)} 个失效" if invalid else "正常", vis[3] - 2)
            status.stylize(STYLE_WARN if invalid else STYLE_DIM if not total else STYLE_OK)
            strat = g.get("routing_strategy") or "-"
            rows.append([
                Text(shorten(g.get("group_name") or "?", vis[0] - 2)),
                Text(shorten(f"{strat} · {strategy_label(strat)}" if strategy_label(strat) else strat, vis[1] - 2)),
                rcell(f"{len(valid)}/{total}", vis[2] - 2),
                status,
            ])
        load_rows(table, rows)
        self.set_subtitle(f"{len(groups)}/{len(self._data['groups'])} 个路由组")
        if not after_layout:
            self.call_after_refresh(lambda: self._rebuild(after_layout=True))
            return
        self.app.status(f"共 {len(self._data['groups'])} 个路由组")

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self._rebuild()

    # ------------------------------------------------------------ 事件

    @on(Input.Changed, "#rt-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(DataTable.RowSelected, "#rt-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._display):
            self._open_form("view", self._display[event.cursor_row])

    @on(Button.Pressed, "#add")
    def _on_add(self) -> None:
        self._open_form("add", None)

    def action_new_group(self) -> None:
        self._open_form("add", None)

    def _open_form(self, mode: str, group) -> None:
        from .routing_form import GroupFormScreen

        def done(ok) -> None:
            if ok:
                self._load()

        self.app.push_screen(GroupFormScreen(mode=mode, group_data=group), done)

    @on(Button.Pressed, "#clean")
    def _on_clean(self) -> None:
        self._clean()

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
                Text(f"{lines}\n确认从 {len(affected)} 个路由组中移除以上 {total_inv} 个失效模型？"),
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
