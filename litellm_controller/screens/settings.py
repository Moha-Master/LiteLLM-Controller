"""设置屏：LiteLLM 连接、Upstream 增删改、重置配置。"""
import asyncio

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Static

from ..client import LiteLLMClient
from ..config import load_config, save_config
from ..upstreams import UPSTREAM_TYPES
from .setup import UpstreamFormModal, build_upstream, fetch_known_providers_for, mask_key
from ..widgets import ConfirmModal, FormField, FormModal, PickItem, PickModal, load_rows, make_table


class SettingsScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self):
        super().__init__()
        self._config: dict = {}
        self._known_providers: list = []

    def compose(self):
        yield Static(Text("设置", style="bold"), classes="page-title")
        yield Static("", id="st-litellm", classes="page-hint")
        with Horizontal(classes="toolbar"):
            yield Button("编辑连接", id="edit_lit")
            yield Button("＋ 添加 Upstream", id="add_up", variant="primary")
            yield Button("重新配置并覆盖", id="reset")
            yield Button("返回", id="back")
        table = make_table("名称", "类型", "Provider 绑定", "模型列表 URL", "Key")
        table.id = "st-table"
        yield table
        yield Static("↑↓ 移动 · 回车 管理所选 Upstream · Esc 返回", classes="page-hint")

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    @work(exclusive=True)
    async def _load(self) -> None:
        try:
            self._config = await asyncio.to_thread(load_config)
        except Exception as e:
            self.app.notify_err(f"配置读取失败: {e}")
            self._config = self.app.config or {}
        lit = self._config.get("litellm", {})
        t = Text()
        t.append("LiteLLM 连接: ", style="bold")
        t.append(f"{lit.get('endpoint', '-')}   key={mask_key(lit.get('key', ''))}")
        self.query_one("#st-litellm", Static).update(t)
        self._rebuild_table()
        if not self._known_providers:
            self.app.status("正在获取 LiteLLM 内置 Provider 列表…")
            self._known_providers = await asyncio.to_thread(
                fetch_known_providers_for, lit
            )

    def _rebuild_table(self) -> None:
        rows = []
        for u in self._config.get("upstreams") or []:
            rows.append([
                Text(u.get("name", "?")),
                Text(UPSTREAM_TYPES.get(u.get("type"), {}).get("label", u.get("type", "?"))),
                Text(u.get("provider") or "未绑定", style="dim" if not u.get("provider") else ""),
                Text(u.get("endpoint", "")),
                Text(mask_key(u.get("key", ""))),
            ])
        load_rows(self.query_one("#st-table", DataTable), rows)

    # ------------------------------------------------------------ 行操作

    @on(DataTable.RowSelected, "#st-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        upstreams = self._config.get("upstreams") or []
        if not (0 <= event.cursor_row < len(upstreams)):
            return
        name = upstreams[event.cursor_row].get("name", "?")
        items = [
            PickItem("edit", "编辑此 Upstream"),
            PickItem("delete", "删除此 Upstream"),
            PickItem("back", "[返回]"),
        ]
        self.app.push_screen(
            PickModal(f"管理 Upstream: {name}", items),
            lambda item: self._row_action(event.cursor_row, item),
        )

    def _row_action(self, index: int, item: PickItem | None) -> None:
        if item is None:
            return
        if item.value == "edit":
            self._edit_upstream(index)
        elif item.value == "delete":
            self._delete_upstream(index)

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

    @on(Button.Pressed, "#add_up")
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
        if index >= len(upstreams):
            return
        existing = upstreams[index]
        self.app.push_screen(
            UpstreamFormModal(self._known_providers, existing=existing),
            lambda data: self._upstream_edited(index, data),
        )

    def _upstream_edited(self, index: int, data: dict | None) -> None:
        if data is None:
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
        if index >= len(upstreams):
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

    # ------------------------------------------------------------ 重置

    @on(Button.Pressed, "#reset")
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

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()
