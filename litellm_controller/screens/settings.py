"""设置屏：LiteLLM 连接、Upstream 增删改（ClickTable 单击 = 编辑）、重置配置。

版式：连接信息 .panel（kv 行 + 编辑按钮）→ 操作 filter-row → Upstream 表格（单滚动条）；
「重新配置并覆盖」为低频危险操作，放入顶栏 ⋮ 折叠菜单。
"""
import asyncio

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static

from ..client import LiteLLMClient
from ..config import load_config, save_config
from ..ui import PageScreen
from ..upstreams import UPSTREAM_TYPES
from ..widgets import (
    ConfirmModal,
    FormField,
    FormModal,
    fit_table_columns,
    load_rows,
    make_table,
    shorten,
)
from .setup import UpstreamFormModal, build_upstream, fetch_known_providers_for, mask_key


class SettingsScreen(PageScreen):
    TITLE = "设置"
    HINT = "↑↓ 移动 · 单击/回车 编辑 Upstream · Ctrl+N 新增 · Ctrl+E 编辑 · Ctrl+D 删除 · Esc/Ctrl+C 返回"

    BINDINGS = [
        Binding("ctrl+n", "new_up", "新增", show=False),
        Binding("ctrl+e", "edit_up", "编辑", show=False),
        Binding("ctrl+d", "delete_up", "删除", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._config: dict = {}
        self._known_providers: list = []
        self._display: list = []
        self.menu = [("重新配置并覆盖（向导）", self._reset, True)]

    # ------------------------------------------------------------ 版式

    def compose_page(self):
        with Vertical(classes="panel"):
            with Horizontal(classes="kv-row"):
                yield Static("Endpoint", classes="kv-label")
                yield Static("-", classes="kv-value", id="st-endpoint")
            with Horizontal(classes="kv-row gap-top"):
                yield Static("Key", classes="kv-label")
                yield Static("-", classes="kv-value", id="st-key")
            with Horizontal(classes="filter-row gap-top"):
                yield Static(classes="fill")
                yield Button("编辑连接", id="edit_lit")
        with Vertical(classes="panel"):
            with Horizontal(classes="filter-row"):
                yield Static("Upstreams", classes="fl-label")
                yield Static(classes="fill")
                yield Button("＋ 添加", id="add_up", variant="primary")
                yield Button("编辑", id="edit_up")
                yield Button("删除", id="del_up", variant="error")
        table = make_table("名称", "类型", "Provider 绑定", "模型列表 URL", "Key")
        table.id = "st-table"
        yield table

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    def reload_page(self) -> None:
        self._load()

    @work(exclusive=True)
    async def _load(self) -> None:
        try:
            self._config = await asyncio.to_thread(load_config)
        except Exception as e:
            self.app.notify_err(f"配置读取失败: {e}")
            self._config = self.app.config or {}
        lit = self._config.get("litellm", {})
        self.query_one("#st-endpoint", Static).update(Text(str(lit.get("endpoint", "-")), style="bold"))
        self.query_one("#st-key", Static).update(Text(mask_key(lit.get("key", "")), style="bold"))
        self._rebuild_table()
        if not self._known_providers:
            self.app.status("正在获取 LiteLLM 内置 Provider 列表…")
            self._known_providers = await asyncio.to_thread(
                fetch_known_providers_for, lit
            )

    def _rebuild_table(self, *, after_layout: bool = False) -> None:
        upstreams = self._config.get("upstreams") or []
        self._display = upstreams
        table = self.query_one("#st-table", DataTable)
        vis = fit_table_columns(table, [16, 14, 14, 30, 12], rows=len(upstreams))
        rows = []
        for u in upstreams:
            rows.append([
                Text(shorten(u.get("name", "?"), vis[0] - 2)),
                Text(shorten(UPSTREAM_TYPES.get(u.get("type"), {}).get("label", u.get("type", "?")), vis[1] - 2)),
                Text(shorten(u.get("provider") or "未绑定", vis[2] - 2)),
                Text(shorten(u.get("endpoint", ""), vis[3] - 2)),
                Text(shorten(mask_key(u.get("key", "")), vis[4] - 2)),
            ])
        load_rows(table, rows)
        self.set_subtitle(f"Upstreams {len(upstreams)} 个")
        if not after_layout:
            self.call_after_refresh(lambda: self._rebuild_table(after_layout=True))

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self._rebuild_table()

    # ------------------------------------------------------------ 行操作

    @on(DataTable.RowSelected, "#st-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        # 一窗到底：直接进入编辑表单，删除按钮集成在表单内
        self._edit_upstream(event.cursor_row)

    @on(Button.Pressed, "#add_up")
    def _on_add_btn(self) -> None:
        self._add_upstream()

    @on(Button.Pressed, "#edit_up")
    def _on_edit_btn(self) -> None:
        self.action_edit_up()

    @on(Button.Pressed, "#del_up")
    def _on_del_btn(self) -> None:
        self.action_delete_up()

    def action_new_up(self) -> None:
        self._add_upstream()

    def action_edit_up(self) -> None:
        self._edit_upstream(self.query_one("#st-table", DataTable).cursor_row)

    def action_delete_up(self) -> None:
        self._delete_upstream(self.query_one("#st-table", DataTable).cursor_row)

    # ------------------------------------------------------------ 连接

    @on(Button.Pressed, "#edit_lit")
    def _edit_litellm(self) -> None:
        lit = self._config.get("litellm", {})
        fields = [
            FormField("endpoint", "Endpoint", value=lit.get("endpoint", ""),
                      validator=lambda v: bool(v and v.strip()), error="endpoint 不能为空"),
            FormField("key", "Key", kind="password", value=lit.get("key", ""),
                      hint="留空保持原 key"),
        ]
        self.app.push_screen(
            FormModal("LiteLLM 连接设置", fields, ok_label="下一步"),
            lambda data: self._litellm_preview(data),
        )

    def _litellm_preview(self, data: dict | None) -> None:
        if data is None:
            return
        lit = self._config.get("litellm", {})
        t = Text()
        t.append(f"Endpoint: {lit.get('endpoint')} → {data['endpoint']}\n")
        t.append("Key     : (已更新)\n" if data["key"] != lit.get("key") else "Key     : (未变更)\n")
        self.app.push_screen(
            ConfirmModal(t, title="确认保存 LiteLLM 连接设置？"),
            lambda ok: self._save_litellm(data) if ok else None,
        )

    def _save_litellm(self, data: dict) -> None:
        config = self._config
        config.setdefault("litellm", {})
        config["litellm"]["endpoint"] = data["endpoint"].strip()
        config["litellm"]["key"] = data["key"] or config["litellm"].get("key", "")
        try:
            save_config(config)
        except Exception as e:
            self.app.notify_err(f"保存失败: {e}")
            return
        self.app.reload_config()
        self._load()
        self._probe()

    @work(exclusive=True)
    async def _probe(self) -> None:
        lit = self._config.get("litellm", {})
        self.app.status("正在测试新连接…")

        def do() -> str:
            try:
                client = LiteLLMClient(lit["endpoint"], lit["key"], timeout=10)
                n = len(client.list_models())
                return f"连接测试成功：当前可用模型 {n} 个"
            except Exception as e:
                return f"连接测试失败: {e}"

        msg = await asyncio.to_thread(do)
        if msg.startswith("连接测试成功"):
            self.app.notify_ok(msg)
        else:
            self.app.notify_err(msg)

    # ------------------------------------------------------------ Upstream 增/改/删

    def _add_upstream(self) -> None:
        self.app.push_screen(
            UpstreamFormModal(self._known_providers),
            lambda data: self._upstream_added(data),
        )

    def _upstream_added(self, data: dict | None) -> None:
        if data is None:
            return
        new = build_upstream(data)
        if any(u["name"] == new["name"] for u in self._config.get("upstreams") or []):
            self.app.notify_err(f"Upstream 名称 [{new['name']}] 已存在，未保存")
            return
        t = Text()
        t.append(f"名称: {new['name']}\n")
        t.append(f"类型: {UPSTREAM_TYPES.get(new['type'], {}).get('label', new['type'])}\n")
        t.append(f"URL : {new['endpoint']}\n")
        t.append(f"Key : {mask_key(new.get('key'))}\n")
        if new.get("provider"):
            t.append(f"绑定: {new['provider']}\n")
        self.app.push_screen(
            ConfirmModal(t, title=f"确认添加 Upstream [{new['name']}] 吗？"),
            lambda ok: self._save_upstream_append(new) if ok else None,
        )

    def _save_upstream_append(self, new: dict) -> None:
        self._config.setdefault("upstreams", []).append(new)
        save_config(self._config)
        self.app.reload_config()
        self._load()
        self.app.notify_ok(f"Upstream [{new['name']}] 已添加并保存")

    def _edit_upstream(self, index: int) -> None:
        upstreams = self._config.get("upstreams") or []
        if index is None or not (0 <= index < len(upstreams)):
            return
        existing = upstreams[index]
        self.app.push_screen(
            UpstreamFormModal(self._known_providers, existing=existing),
            lambda data: self._upstream_edited(index, data),
        )

    def _upstream_edited(self, index: int, data) -> None:
        if data is None:
            return
        if data == "frm-delete":
            self._delete_upstream(index)
            return
        existing = self._config["upstreams"][index]
        new = build_upstream(data, existing=existing)
        if any(
            u["name"] == new["name"]
            for i, u in enumerate(self._config["upstreams"]) if i != index
        ):
            self.app.notify_err(f"Upstream 名称 [{new['name']}] 已存在，未保存")
            return
        t = Text()
        has_diff = False
        for field in ("name", "type", "endpoint", "provider"):
            old_v = existing.get(field)
            new_v = new.get(field)
            if old_v != new_v:
                has_diff = True
                t.append(f"{field:10}: {old_v} → {new_v}\n")
        if existing.get("key") != new.get("key"):
            has_diff = True
            t.append("key       : (已更新)\n")
        if not has_diff:
            t.append("（参数无变更）\n")
        self.app.push_screen(
            ConfirmModal(t, title=f"确认保存 Upstream [{existing['name']}] 的修改？"),
            lambda ok: self._save_upstream_edit(index, new) if ok else None,
        )

    def _save_upstream_edit(self, index: int, new: dict) -> None:
        self._config["upstreams"][index] = new
        save_config(self._config)
        self.app.reload_config()
        self._load()
        self.app.notify_ok(f"Upstream [{new['name']}] 已更新并保存")

    def _delete_upstream(self, index: int) -> None:
        upstreams = self._config.get("upstreams") or []
        if index is None or index >= len(upstreams):
            return
        name = upstreams[index]["name"]
        self.app.push_screen(
            ConfirmModal(f"确认删除 Upstream [{name}]？", title="删除 Upstream",
                         default_yes=False, yes="确认删除"),
            lambda ok: self._save_upstream_delete(index, name) if ok else None,
        )

    def _save_upstream_delete(self, index: int, name: str) -> None:
        self._config["upstreams"].pop(index)
        save_config(self._config)
        self.app.reload_config()
        self._load()
        self.app.notify_ok(f"Upstream [{name}] 已删除")

    # ------------------------------------------------------------ 重置（折叠菜单）

    def _reset(self) -> None:
        self.app.push_screen(
            ConfirmModal(
                "重新配置将覆盖现有 config.yaml 中的全部设置，确定继续？",
                title="重新配置", default_yes=False, yes="确定继续",
            ),
            lambda ok: self._open_setup() if ok else None,
        )

    @work(exclusive=True)
    async def _open_setup(self) -> None:
        from .setup import SetupWizardScreen
        saved = await self.app.push_screen_wait(
            SetupWizardScreen(reason="重新配置向导", config_missing=False)
        )
        if saved:
            self.app.reload_config()
            self._known_providers = []
            self._load()
        else:
            self.app.status("已取消重新配置")
