"""模型管理屏：列表（DataTable + 模糊搜索 + 排序）、行操作、编辑表单。

操作结果通过 App 状态栏 + notify 反馈，替代旧版 print。
"""
import asyncio
from collections import Counter

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Select, Static

from ..client import LiteLLMError
from ..config import cost_map_providers
from ..modeldata import (
    cost_text,
    credential_display,
    duplicate_name_counts,
    fmt_cost,
    model_id_of,
    sorted_models,
)
from ..widgets import (
    STYLE_DIM,
    STYLE_ERR,
    STYLE_OK,
    ConfirmModal,
    FormField,
    FormModal,
    PickItem,
    PickModal,
    filter_fuzzy,
    load_rows,
    make_table,
)

SORT_OPTIONS = [
    ("public", "Public Name"),
    ("litellm", "LiteLLM Name"),
    ("provider", "Provider"),
    ("credential", "Credential"),
    ("status", "状态"),
]


def _nonempty(v: str) -> bool:
    return bool(v and v.strip())


def _cost_ok(v: str) -> bool:
    v = (v or "").strip()
    if not v:
        return True
    try:
        return float(v) >= 0
    except ValueError:
        return False


class ModelListScreen(Screen):
    """模型列表页。"""

    BINDINGS = [
        Binding("escape", "back", "返回", priority=False),
        Binding("/", "focus_search", "搜索", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._models: list = []
        self._display: list = []
        self._sort_key = "public"
        self._filter = ""

    def compose(self):
        yield Static(Text("模型管理", style="bold"), classes="page-title", id="ml-title")
        with Horizontal(classes="search-row"):
            yield Static("搜索  ", classes="page-hint")
            yield Input(placeholder="按 Public/LiteLLM 名称筛选（/ 聚焦）", id="ml-search")
        with Horizontal(classes="toolbar"):
            yield Button("＋ 添加模型", id="add", variant="primary")
            yield Button("模型参数管理", id="meta")
            yield Button("刷新", id="refresh")
            yield Select(
                [(label, key) for key, label in SORT_OPTIONS],
                value="public",
                allow_blank=False,
                id="ml-sort",
            )
            yield Button("返回", id="back")
        table = make_table(
            "Public Name",
            "LiteLLM Name",
            "Provider",
            "Cost in/out ($/1M)",
            "Credential",
            "状态",
            cursor="row",
        )
        table.id = "ml-table"
        yield table
        yield Static("↑↓ 移动 · 回车 操作所选 · r 刷新 · Esc 返回", classes="page-hint")

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    # ------------------------------------------------------------ 数据加载

    @work(exclusive=True)
    async def _load(self, silent: bool = False) -> None:
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
        self.app.status(f"共 {len(models)} 个模型")

    def _rebuild(self) -> None:
        models = sorted_models(self._models, self._sort_key)
        if self._filter:
            models = filter_fuzzy(
                models, self._filter, lambda m: m.get("model_name", "")
            )
        self._display = models
        counts = duplicate_name_counts(models)
        seen = Counter()
        rows = []
        for m in models:
            info = m.get("model_info") or {}
            params = m.get("litellm_params") or {}
            name = m.get("model_name", "?")
            seen[name] += 1
            pub = Text(name)
            if counts[name] > 1:
                pub.append(f"  [{seen[name]}/{counts[name]}]", style=STYLE_DIM)
            blocked = bool(info.get("blocked", False))
            status = Text("🔴 禁用", style=STYLE_ERR) if blocked else Text("🟢 启用", style=STYLE_OK)
            rows.append([
                pub,
                Text(params.get("model", "?")),
                Text(params.get("custom_llm_provider") or "-"),
                Text(cost_text(info)),
                Text(params.get("litellm_credential_name") or "-"),
                status,
            ])
        table = self.query_one("#ml-table", DataTable)
        load_rows(table, rows)
        self.query_one("#ml-title", Static).update(
            Text(f"模型管理（{len(self._display)}/{len(self._models)}）", style="bold")
        )

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
        self._run_wizard()

    @on(Button.Pressed, "#meta")
    def _on_meta(self, event: Button.Pressed) -> None:
        from .meta import MetaHomeScreen
        self.app.push_screen(MetaHomeScreen())

    @on(Button.Pressed, "#refresh")
    def _on_refresh(self, event: Button.Pressed) -> None:
        self._load()

    @on(Button.Pressed, "#back")
    def _on_back(self, event: Button.Pressed) -> None:
        self.app.pop_screen()

    @on(DataTable.RowSelected, "#ml-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self._display):
            self._show_actions(self._display[idx])

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_focus_search(self) -> None:
        self.query_one("#ml-search", Input).focus()

    def key_r(self) -> None:
        if self.app.focused is None or isinstance(self.app.focused, Input):
            return
        self._load()

    # ------------------------------------------------------------ 单模型操作

    def _show_actions(self, model: dict) -> None:
        info = model.get("model_info") or {}
        blocked = bool(info.get("blocked", False))
        items = [
            PickItem("edit", "编辑"),
            PickItem("toggle", "禁用" if not blocked else "启用"),
            PickItem("delete", "删除"),
            PickItem("back", "[返回列表]"),
        ]
        name = model.get("model_name", "?")
        params = model.get("litellm_params") or {}
        msg = (
            f"provider: {params.get('custom_llm_provider', '?')}  ·  "
            f"credential: {params.get('litellm_credential_name', '无')}  ·  "
            f"cost: {cost_text(info)}  ·  状态: {'🔴 已禁用' if blocked else '🟢 启用中'}"
        )
        self.app.push_screen(
            PickModal(f"模型: {name}", items, message=msg),
            lambda item: self._on_action(item, model),
        )

    def _on_action(self, item: PickItem | None, model: dict) -> None:
        if item is None or item.value == "back":
            return
        if item.value == "edit":
            self._edit_model(model)
        elif item.value == "toggle":
            self._toggle(model)
        elif item.value == "delete":
            self._delete(model)

    @work(exclusive=True)
    async def _run_wizard(self) -> None:
        from .model_wizard import ModelWizardScreen
        result = await self.app.push_screen_wait(ModelWizardScreen())
        if result:
            self._load()

    # ------------------------------------------------------------ 禁用/启用

    def _toggle(self, model: dict) -> None:
        info = model.get("model_info") or {}
        if not info.get("id"):
            self.app.notify_warn("该模型没有数据库 ID，无法禁用/启用")
            return
        blocked = bool(info.get("blocked", False))
        action = "禁用" if blocked else "启用"
        name = model.get("model_name", "?")
        self.app.push_screen(
            ConfirmModal(f"确认{action}模型 [{name}]？", title=f"{action}模型"),
            lambda ok: self._do_toggle(model, not blocked, action) if ok else None,
        )

    @work(exclusive=True)
    async def _do_toggle(self, model: dict, blocked: bool, action: str) -> None:
        client = self.app.get_client()
        mid = model_id_of(model)
        try:
            await asyncio.to_thread(lambda: client.update_model(mid, blocked=blocked))
            self.app.notify_ok(f"模型 {model.get('model_name')} 已{action}")
            self._load()
        except LiteLLMError as e:
            self.app.notify_err(f"{action}失败: {e}")

    # ------------------------------------------------------------ 删除

    def _delete(self, model: dict) -> None:
        if not (model.get("model_info") or {}).get("id"):
            self.app.notify_warn("该模型没有数据库 ID，无法删除")
            return
        name = model.get("model_name", "?")
        self.app.push_screen(
            ConfirmModal(
                f"确认删除模型 {name}？此操作不可恢复。",
                title="删除模型", default_yes=False, yes="确认删除",
            ),
            lambda ok: self._do_delete(model) if ok else None,
        )

    @work(exclusive=True)
    async def _do_delete(self, model: dict) -> None:
        client = self.app.get_client()
        try:
            await asyncio.to_thread(lambda: client.delete_model(model_id_of(model)))
            self.app.notify_ok(f"模型 {model.get('model_name')} 已删除")
            self._load()
        except LiteLLMError as e:
            self.app.notify_err(f"删除失败: {e}")

    # ------------------------------------------------------------ 编辑表单

    @work(exclusive=True)
    async def _edit_model(self, model: dict) -> None:
        info = model.get("model_info") or {}
        if not info.get("id"):
            self.app.notify_warn("该模型没有数据库 ID，无法编辑")
            return
        params = model.get("litellm_params") or {}
        client = self.app.get_client()
        cost_map = await self.app.get_cost_map()
        try:
            credentials = await asyncio.to_thread(client.list_credentials)
        except LiteLLMError:
            credentials = []

        cur_provider = params.get("custom_llm_provider") or ""
        cur_credential = params.get("litellm_credential_name") or ""
        providers = cost_map_providers(cost_map)
        cred_rows = credential_display(credentials)

        def provider_items():
            ordered = ([cur_provider] + [p for p in providers if p != cur_provider]) if cur_provider in providers else providers
            return [PickItem(p, p, hint="当前" if p == cur_provider else "") for p in ordered]

        def credential_items():
            ordered = ([c for c in cred_rows if c["name"] == cur_credential] +
                       [c for c in cred_rows if c["name"] != cur_credential]) if cur_credential in {c["name"] for c in cred_rows} else cred_rows
            items = [
                PickItem(c["name"], c["name"], hint=f"{c['provider']} · {c['key']}" + (" · 当前" if c["name"] == cur_credential else ""))
                for c in ordered
            ]
            items.append(PickItem("", "[不绑定]"))
            return items

        fields = [
            FormField("model_name", "Public Name", value=model.get("model_name", ""),
                      validator=_nonempty, error="不能为空"),
            FormField("litellm_model", "LiteLLM Name", value=params.get("model", ""),
                      validator=_nonempty, error="不能为空"),
            FormField("provider", "Provider", kind="browse", value=cur_provider,
                      pick_items=provider_items, pick_title="选择 Provider"),
            FormField("credential", "Credential", kind="browse", value=cur_credential,
                      pick_items=credential_items, pick_title="选择 Credential",
                      hint="可留空表示不绑定"),
            FormField("in_cost", "Input $/1M", value=fmt_cost(info.get("input_cost_per_token")),
                      validator=_cost_ok, error="请输入非负数字", hint="留空不修改"),
            FormField("out_cost", "Output $/1M", value=fmt_cost(info.get("output_cost_per_token")),
                      validator=_cost_ok, error="请输入非负数字", hint="留空不修改"),
            FormField("cache_read", "Cache Read $/1M",
                      value=fmt_cost(info.get("cache_read_input_token_cost")),
                      validator=_cost_ok, error="请输入非负数字", hint="留空不修改"),
            FormField("cache_write", "Cache Write $/1M",
                      value=fmt_cost(info.get("cache_creation_input_token_cost")),
                      validator=_cost_ok, error="请输入非负数字", hint="留空不修改"),
        ]
        data = await self.app.push_screen_wait(
            FormModal(f"编辑模型: {model.get('model_name', '?')}", fields, ok_label="下一步")
        )
        if data is None:
            return

        model_info = {}
        cost_fields = {
            "in_cost": "input_cost_per_token",
            "out_cost": "output_cost_per_token",
            "cache_read": "cache_read_input_token_cost",
            "cache_write": "cache_creation_input_token_cost",
        }
        for qname, field in cost_fields.items():
            val = (data.get(qname) or "").strip()
            if val:
                model_info[field] = round(float(val) / 1e6, 12)

        summary = (
            f"model name: {data['model_name'].strip()}\n"
            f"litellm model: {data['litellm_model'].strip()}\n"
            f"provider: {data['provider']}\n"
            f"credential: {data['credential'] or '无'}\n"
            f"cost: {'不修改' if not model_info else f'{len(model_info)} 项'}"
        )
        ok = await self.app.push_screen_wait(
            ConfirmModal(summary, title="确认修改？", default_yes=True)
        )
        if not ok:
            self.app.status("已取消修改")
            return

        litellm_params = {
            "model": data["litellm_model"].strip(),
            "custom_llm_provider": data["provider"].strip(),
        }
        if data["credential"]:
            litellm_params["litellm_credential_name"] = data["credential"].strip()
        try:
            await asyncio.to_thread(lambda: client.update_model(
                model_id_of(model),
                model_name=data["model_name"].strip(),
                litellm_params=litellm_params,
                model_info=model_info or None,
            ))
            self.app.notify_ok(f"模型已更新: {data['model_name'].strip()}")
            self._load()
        except LiteLLMError as e:
            self.app.notify_err(f"更新失败: {e}")
