"""模型管理屏：列表（ClickTable + 模糊搜索 + 排序）、单击/回车进入表单、批量添加入口。

版式：内容区 = 小容器（.panel：搜索 / 排序 / 主操作按钮）+ 大容器（.tbl 表格，1fr 单滚动条）。
操作结果通过 App 状态栏 + notify 反馈。
"""
import asyncio
from collections import Counter

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Select, Static

from ..client import LiteLLMError
from ..modeldata import (
    cost_text,
    duplicate_name_counts,
    sorted_models,
)
from ..ui import PageScreen
from ..widgets import (
    STYLE_DIM,
    STYLE_ERR,
    STYLE_OK,
    filter_fuzzy,
    fit_table_columns,
    load_rows,
    make_table,
    rcell,
    shorten,
)

SORT_OPTIONS = [
    ("public", "Public Name"),
    ("litellm", "LiteLLM Name"),
    ("provider", "Provider"),
    ("credential", "Credential"),
    ("status", "状态"),
]

NAME_CAP = 24  # Public / LiteLLM 名称显示宽度上限


class ModelListScreen(PageScreen):
    """模型列表页：单击 / 回车进入模型详情（查看 / 编辑）。"""

    TITLE = "模型管理"
    HINT = "↑↓ 移动 · 单击/回车 查看编辑 · Ctrl+N 添加 · / 搜索 · Ctrl+R 刷新 · Esc/Ctrl+C 返回"

    BINDINGS = [
        Binding("/", "focus_search", "搜索", show=False),
        Binding("ctrl+n", "new_model", "添加", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._models: list = []
        self._display: list = []
        self._sort_key = "public"
        self._filter = ""

    def compose_page(self):
        with Vertical(classes="panel"):
            with Horizontal(classes="filter-row"):
                yield Static("搜索", classes="fl-label")
                yield Input(placeholder="按 Public / LiteLLM 名称筛选（/ 聚焦）", compact=True, id="ml-search")
            with Horizontal(classes="filter-row gap-top"):
                yield Static("排序", classes="fl-label")
                yield Select(
                    [(label, key) for key, label in SORT_OPTIONS],
                    value="public",
                    allow_blank=False,
                    compact=True,
                    id="ml-sort",
                )
                yield Static(classes="fill")
                yield Button("＋ 添加模型", id="add", variant="primary")
                yield Button("模型参数管理", id="meta")
        table = make_table(
            "Public Name",
            "LiteLLM Name",
            "Provider",
            "Cost in/out ($/1M)",
            "Credential",
            "状态",
        )
        table.id = "ml-table"
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
        self.app.status("正在获取模型列表…")
        try:
            models = await asyncio.to_thread(client.list_models)
        except LiteLLMError as e:
            self.app.notify_err(f"获取模型列表失败: {e}")
            self._models = []
            self._rebuild()
            return
        self._models = models
        self._rebuild()

    def _rebuild(self, *, after_layout: bool = False) -> None:
        models = sorted_models(self._models, self._sort_key)
        if self._filter:
            models = filter_fuzzy(
                models, self._filter, lambda m: m.get("model_name", "")
            )
        self._display = models
        counts = duplicate_name_counts(models)
        seen = Counter()
        table = self.query_one("#ml-table", DataTable)
        vis = fit_table_columns(table, [24, 24, 12, 14, 14, 8], rows=len(models))
        rows = []
        for m in models:
            info = m.get("model_info") or {}
            params = m.get("litellm_params") or {}
            name = m.get("model_name", "?")
            seen[name] += 1
            pub = Text(shorten(name, min(NAME_CAP, vis[0] - 2)))
            if counts[name] > 1:
                pub.append(f"  [{seen[name]}/{counts[name]}]", style=STYLE_DIM)
            blocked = bool(info.get("blocked", False))
            status = rcell("禁用" if blocked else "启用", vis[5] - 2)
            status.stylize(STYLE_ERR if blocked else STYLE_OK)
            rows.append([
                pub,
                Text(shorten(params.get("model", "?"), vis[1] - 2)),
                Text(shorten(params.get("custom_llm_provider") or "-", vis[2] - 2)),
                rcell(cost_text(info), vis[3] - 2),
                Text(shorten(params.get("litellm_credential_name") or "-", vis[4] - 2)),
                status,
            ])
        load_rows(table, rows)
        self.set_subtitle(f"{len(self._display)}/{len(self._models)} 个模型")
        if not after_layout:
            # 布局稳定后整体重建一次：列宽、表头、单元格基于同一份宽度，避免错位
            self.call_after_refresh(lambda: self._rebuild(after_layout=True))
            return
        self.app.status(f"共 {len(self._models)} 个模型")

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self._rebuild()

    # ------------------------------------------------------------ 事件

    @on(Input.Changed, "#ml-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(Select.Changed, "#ml-sort")
    def _on_sort(self, event: Select.Changed) -> None:
        if event.value and event.value != Select.NULL:
            self._sort_key = str(event.value)
            self._rebuild()

    @on(Button.Pressed, "#add")
    def _on_add(self, event: Button.Pressed) -> None:
        self._open_form("add", None)

    def action_new_model(self) -> None:
        self._open_form("add", None)

    @on(Button.Pressed, "#meta")
    def _on_meta(self, event: Button.Pressed) -> None:
        from .meta import MetaHomeScreen
        self.app.push_screen(MetaHomeScreen())

    def _open_form(self, mode: str, model_data: dict | None) -> None:
        from .model_form import ModelFormScreen

        def done(ok: bool) -> None:
            if ok:
                self._load()

        self.app.push_screen(ModelFormScreen(mode=mode, model_data=model_data), done)

    @on(DataTable.RowSelected, "#ml-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self._display):
            self._open_form("view", self._display[idx])

    def action_focus_search(self) -> None:
        self.query_one("#ml-search", Input).focus()
