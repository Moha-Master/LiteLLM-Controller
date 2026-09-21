"""合一型路由组表单页面：支持路由组添加、只读查看、就地编辑。"""
import asyncio

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, SelectionList, Static
from textual.widgets.selection_list import Selection

from ..widgets import ConfirmModal, busy, filter_fuzzy


class GroupFormScreen(ModalScreen[bool]):
    """一体化路由组管理页面。"""

    DEFAULT_CSS = """
    GroupFormScreen {
        align: center middle;
    }
    GroupFormScreen > .modal-box {
        width: 100;
        height: 88%;
    }
    #form-body {
        height: 1fr;
        padding: 1 2;
    }
    .form-row {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }
    .form-label {
        width: 15;
        height: 1;
        content-align: left middle;
        text-style: bold;
    }
    .form-select, .form-input {
        width: 1fr;
    }
    #member-section {
        border: round $secondary;
        padding: 1;
        height: 14;
        margin-bottom: 1;
        layout: vertical;
    }
    #member-search {
        margin-bottom: 1;
    }
    #member-list {
        height: 1fr;
        border: round $surface;
    }
    """

    BINDINGS = [Binding("escape,ctrl+c", "back", "返回", show=False)]

    STRATEGIES = [
        ("简单随机 (simple-shuffle)", "simple-shuffle"),
        ("轮询 (round-robin)", "round-robin"),
        ("最小并发/最空闲 (least-busy)", "least-busy"),
        ("低成本/低用量优先 (usage-based-routing-v2)", "usage-based-routing-v2"),
        ("最低延迟 (latency-based-routing)", "latency-based-routing"),
        ("最低成本 (cost-based-routing)", "cost-based-routing"),
    ]

    def __init__(self, mode: str = "add", group_data: dict | None = None):
        super().__init__()
        self.mode = mode
        self.group_data = group_data or {}
        self._all_models = []  # 缓存所有模型名用于筛选
        self._model_to_group: dict[str, str] = {}  # 记录模型被其他哪个路由组占用: {model_name: group_name}
        self._selected_members = set(self.group_data.get("models", []))

    def compose(self) -> ComposeResult:
        title_map = {
            "add": "创建新路由组",
            "view": "路由组详情",
            "edit": "修改路由组配置",
        }
        with Vertical(classes="modal-box"):
            yield Static(Text(title_map[self.mode], style="bold"), classes="modal-title")
            with VerticalScroll(id="form-body"):
                # Group Name
                with Horizontal(classes="form-row"):
                    yield Static("组名称:", classes="form-label")
                    yield Input(self.group_data.get("group_name", ""), compact=True, id="frm-group-name",
                                classes="form-input", placeholder="如: gpt-4-cluster")

                # Strategy
                with Horizontal(classes="form-row"):
                    yield Static("路由策略:", classes="form-label")
                    yield Select(self.STRATEGIES, value=self.group_data.get("routing_strategy", "simple-shuffle"),
                                 compact=True, id="frm-strategy", classes="form-select")

                # Members Selection
                with Vertical(id="member-section"):
                    yield Label("成员模型勾选 (勾选即加入该组):", classes="form-label")
                    yield Input(placeholder="输入模型名称实时筛选...", compact=True, id="member-search")
                    yield SelectionList(id="member-list")

            # Bottom Controls（标准顺序：取消在左、主操作、危险最右）
            with Horizontal(classes="btn-row", id="bottom-controls"):
                if self.mode == "add":
                    yield Button("取消", id="btn-cancel", variant="default")
                    yield Button("保存创建", id="btn-save", variant="primary")
                elif self.mode == "view":
                    yield Button("返回列表", id="btn-back", variant="default")
                    yield Button("切换为编辑模式", id="btn-switch-edit", variant="primary")
                elif self.mode == "edit":
                    yield Button("取消", id="btn-cancel", variant="default")
                    yield Button("保存修改", id="btn-save", variant="primary")
                    yield Button("清理失效成员", id="btn-clean", variant="warning")
                    yield Button("删除此路由组", id="btn-delete", variant="error")

    async def on_mount(self) -> None:
        await self._load_all_models()
        self._rebuild_list()

        if self.mode == "view":
            self._set_read_only(True)

    def action_back(self) -> None:
        self.dismiss(False)

    # ------------------------------------------------------------ 逻辑处理

    async def _load_all_models(self) -> None:
        sel = self.query_one("#member-list", SelectionList)
        sel.loading = True
        client = self.app.get_client()
        try:
            models = await asyncio.to_thread(client.list_models)
            self._all_models = [m.get("model_name") for m in models if m.get("model_name")]
            self._all_models = sorted(set(self._all_models))

            # 读取路由组设置，查找已被其他组占用的模型
            router_settings = await asyncio.to_thread(client.get_router_settings)
            groups = (router_settings.get("current_values") or {}).get("routing_groups") or []
            cur_group_name = self.group_data.get("group_name")
            self._model_to_group = {}
            for g in groups:
                gname = g.get("group_name")
                if gname and gname != cur_group_name:
                    for m in g.get("models") or []:
                        self._model_to_group[m] = gname
        except Exception as e:
            self.app.notify_err(f"加载模型列表失败: {e}")
            self._all_models = []
            self._model_to_group = {}
        finally:
            sel.loading = False

    def _rebuild_list(self, filter_text: str = "") -> None:
        sel = self.query_one("#member-list", SelectionList)
        # 获取当前所有的勾选状态，合并到缓存中
        self._selected_members.update(set(sel.selected))

        # 筛选
        filtered = filter_fuzzy(self._all_models, filter_text, lambda m: m)

        options = []
        for m in filtered:
            # 标记是否已被当前组选中
            is_selected = m in self._selected_members
            other_group = self._model_to_group.get(m)
            if other_group and not is_selected:
                label = Text(f"{m} (已加入: {other_group})", style="dim")
                options.append(Selection(label, m, False, disabled=True))
            else:
                options.append(Selection(m, m, is_selected))

        sel.clear_options()
        sel.add_options(options)

    def _set_read_only(self, read_only: bool) -> None:
        self.query_one("#frm-group-name", Input).disabled = (read_only or self.mode == "view")
        self.query_one("#frm-strategy", Select).disabled = read_only
        self.query_one("#member-search", Input).disabled = read_only
        self.query_one("#member-list", SelectionList).disabled = read_only

    @on(Input.Changed, "#member-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self._rebuild_list(event.value.strip())

    @on(SelectionList.SelectedChanged, "#member-list")
    def _on_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        # 同步增量更新
        self._selected_members.update(set(event.selection_list.selected))
        currently_selected = set(event.selection_list.selected)
        for opt in event.selection_list.options:
            if not opt.disabled and opt.value not in currently_selected:
                self._selected_members.discard(opt.value)

    @on(Button.Pressed, "#btn-switch-edit")
    def _switch_edit_mode(self) -> None:
        self.mode = "edit"
        self._set_read_only(False)
        ctrls = self.query_one("#bottom-controls")
        ctrls.remove_children()
        ctrls.mount(Button("取消", id="btn-cancel", variant="default"))
        ctrls.mount(Button("保存修改", id="btn-save", variant="primary"))
        ctrls.mount(Button("清理失效成员", id="btn-clean", variant="warning"))
        ctrls.mount(Button("删除此路由组", id="btn-delete", variant="error"))

    @on(Button.Pressed, "#btn-cancel")
    @on(Button.Pressed, "#btn-back")
    def _on_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-clean")
    def _on_clean(self) -> None:
        self.app.push_screen(
            ConfirmModal("确定从该组中清理掉所有在模型列表中已不存在的失效成员吗？", title="清理失效成员"),
            lambda ok: self._do_clean() if ok else None,
        )

    def _do_clean(self) -> None:
        valid_set = set(self._all_models)
        self._selected_members = {m for m in self._selected_members if m in valid_set}
        self._rebuild_list(self.query_one("#member-search", Input).value)
        self.app.notify_ok("已在预览中清理失效成员，请点击保存生效")

    @on(Button.Pressed, "#btn-delete")
    def _on_delete_press(self) -> None:
        name = self.group_data.get("group_name", "?")
        self.app.push_screen(
            ConfirmModal(f"确认删除路由组 {name}？", title="删除路由组", default_yes=False, yes="确认删除"),
            lambda ok: self._do_delete() if ok else None
        )

    @work(exclusive=True)
    async def _do_delete(self) -> None:
        client = self.app.get_client()
        name = self.group_data.get("group_name")
        try:
            # 路由组删除通常是更新设置，将其从列表移除
            settings = await asyncio.to_thread(client.get_router_settings)
            groups = settings.get("current_values", {}).get("routing_groups") or []
            new_groups = [g for g in groups if g.get("group_name") != name]
            await asyncio.to_thread(lambda: client.update_router_settings({"routing_groups": new_groups}))
            self.app.notify_ok(f"路由组 {name} 已删除")
            self.dismiss(True)
        except Exception as e:
            self.app.notify_err(f"删除失败: {e}")

    @on(Button.Pressed, "#btn-save")
    def _on_save_press(self) -> None:
        name = self.query_one("#frm-group-name", Input).value.strip()
        if not name:
            self.app.notify_err("路由组名称不能为空")
            return

        strat = self.query_one("#frm-strategy", Select).value
        if not strat or strat is Select.NULL:
            strat = "simple-shuffle"

        models = sorted(self._selected_members)
        if not models:
            self.app.notify_err("请至少勾选一个成员模型")
            return

        self._do_save(name, str(strat), models)

    @work(exclusive=True)
    async def _do_save(self, name: str, strategy: str, models: list[str]) -> None:
        client = self.app.get_client()
        try:
            async with busy(self.app, "正在保存路由组…", mode="dots"):
                settings = await asyncio.to_thread(client.get_router_settings)
                groups = settings.get("current_values", {}).get("routing_groups") or []

                # 如果是 edit 模式，先移除旧的
                if self.mode == "edit":
                    old_name = self.group_data.get("group_name")
                    groups = [g for g in groups if g.get("group_name") != old_name]

                # 检查同名冲突 (add 模式)
                if self.mode == "add" and any(g.get("group_name") == name for g in groups):
                    self.app.notify_err(f"路由组名称 {name} 已存在")
                    return

                # 添加/更新
                groups.append({
                    "group_name": name,
                    "models": models,
                    "routing_strategy": strategy
                })

                await asyncio.to_thread(lambda: client.update_router_settings({"routing_groups": groups}))
                self.app.notify_ok(f"路由组 {name} 已保存")
            self.dismiss(True)
        except Exception as e:
            self.app.notify_err(f"保存失败: {e}")
