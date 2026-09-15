"""路由组添加/编辑向导：① 名称 → ② 模型 → ③ 路由策略 → ④ 确认。

沿用统一操作逻辑：从确认页跳入的步骤，完成后一律回到确认页。
"""
import asyncio

from rich.text import Text
from textual import work
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..client import LiteLLMError
from .routing import fresh_groups, load_routing_data, model_membership, strategy_label
from ..widgets import InputModal, MultiPickModal, PickItem, PickModal

NAME = "__name__"
STRATEGY = "__strategy__"
SUBMIT = "__submit__"
CANCEL = "__cancel__"


class GroupWizardScreen(ModalScreen[bool]):
    """group=None 为添加，否则为编辑。True = 已应用。"""

    DEFAULT_CSS = """
    GroupWizardScreen { align: center middle; }
    GroupWizardScreen > .wizard-info { width: 80; height: auto; }
    """

    def __init__(self, group: dict | None = None):
        super().__init__()
        self.editing = group is not None
        orig = group or {}
        self.state = {
            "orig_name": orig.get("group_name"),
            "orig_models": list(orig.get("models") or []),
            "orig_strategy": orig.get("routing_strategy"),
            "orig_args": orig.get("routing_strategy_args"),
            "name": orig.get("group_name") or "",
            "models": list(orig.get("models") or []),
            "strategy": orig.get("routing_strategy") or "simple-shuffle",
        }
        self._data: dict | None = None

    def compose(self):
        with Vertical(classes="modal-box wizard-info"):
            yield Static(
                Text("编辑路由组" if self.editing else "添加路由组", style="bold"),
                classes="modal-title",
            )
            yield Static(
                "步骤：① 名称 → ② 模型 → ③ 路由策略 → ④ 确认 · Esc 取消向导",
                id="gw-hint", classes="page-hint",
            )

    def on_mount(self) -> None:
        self._flow()

    async def _load_data(self):
        client = self.app.get_client()
        self.app.status("正在获取路由数据…")
        try:
            self._data = await asyncio.to_thread(load_routing_data, client)
        except LiteLLMError as e:
            self.app.notify_err(f"获取数据失败: {e}")
            self._data = None

    @work(exclusive=True)
    async def _flow(self) -> None:
        jump_back = False
        step = 1
        while True:
            await self._load_data()
            if self._data is None:
                self.dismiss(False)
                return
            if step == 4:
                nxt = await self._step_review()
                if nxt is None:
                    self.dismiss(False)
                    return
                if nxt >= 5:
                    self.dismiss(True)
                    return
                step = nxt
                jump_back = True
                continue
            if step == 1:
                nxt = await self._step_name()
            elif step == 2:
                nxt = await self._step_models()
            else:
                nxt = await self._step_strategy()
            if nxt is None:
                self.dismiss(False)
                return
            if jump_back:
                jump_back = False
                step = 4
                continue
            step = nxt

    # ------------------------------------------------------------ ① 名称

    async def _step_name(self):
        data = self._data
        orig = self.state["orig_name"] if self.editing else None
        other = {g.get("group_name") for g in data["groups"] if g.get("group_name") != orig}

        def validate(v: str) -> bool:
            v = (v or "").strip()
            return bool(v and v != "default" and v not in other)

        prompt = "路由组名称（修改）:" if self.editing else "请输入路由组名称:"
        name = await self.app.push_screen_wait(
            InputModal(
                prompt, title="① 名称", value=self.state["name"] or (orig or ""),
                validator=validate,
                error="名称不能为空，default 为保留字，且不能与现有路由组重名",
            )
        )
        if not name:
            return None if self.editing else None
        self.state["name"] = name.strip()
        if name.strip() in data["models_by_name"]:
            self.app.notify_warn(f"'{name.strip()}' 已是模型名，调用该模型将产生歧义")
        return 2

    # ------------------------------------------------------------ ② 模型

    async def _step_models(self):
        data = self._data
        models_by_name = data["models_by_name"]
        membership = model_membership(data["groups"])
        orig = self.state["orig_name"]
        current = set(self.state["models"])
        if not models_by_name:
            self.app.notify_warn("模型列表为空，无法选择模型")
            return 1 if not self.editing else 3
        items = []
        for name in sorted(models_by_name):
            owner = membership.get(name)
            if owner and owner != orig:
                items.append(PickItem(name, name, disabled=True, hint=f"已属于 {owner}"))
            else:
                items.append(PickItem(name, name, preselected=name in current))
        picked = await self.app.push_screen_wait(
            MultiPickModal(
                "② 选择模型（空格勾选）", items,
                message=f"当前已选 {len(current)} 个；他组成员已锁定",
            )
        )
        if picked is None:
            return 1
        self.state["models"] = [m for m in picked if isinstance(m, str)]
        if not self.state["models"]:
            self.app.notify_warn("请至少选择一个模型")
            return 2
        return 3

    # ------------------------------------------------------------ ③ 策略

    async def _step_strategy(self):
        strategies = self._data["strategies"]
        cur = self.state["strategy"]
        ordered = ([cur] + [s for s in strategies if s != cur]) if cur in strategies else list(strategies)
        items = [
            PickItem(s, s, hint=(strategy_label(s) or "") + (" · 当前" if s == cur else ""))
            for s in ordered
        ]
        footer = [PickItem("__back__", "← 上一步")]
        picked = await self.app.push_screen_wait(
            PickModal("③ 选择路由策略", items, footer_items=footer)
        )
        if picked is None or picked.value == "__back__":
            return 2
        self.state["strategy"] = str(picked.value)
        return 4

    # ------------------------------------------------------------ ④ 确认

    async def _step_review(self):
        state = self.state
        while True:
            editing = self.editing
            added = [m for m in state["models"] if m not in state["orig_models"]] if editing else list(state["models"])
            removed = [m for m in state["orig_models"] if m not in state["models"]] if editing else []
            items = []
            name_label = f"名称: {state['name']}"
            if editing and state["name"] != state["orig_name"]:
                name_label += f"  (原: {state['orig_name']})"
            items.append(PickItem(NAME, name_label))
            strat_label = f"路由策略: {state['strategy']}"
            if editing and state["strategy"] != state["orig_strategy"]:
                strat_label += f"  (原: {state['orig_strategy']})"
            items.append(PickItem(STRATEGY, strat_label))
            added_set = set(added)
            for m in state["models"]:
                mark = "+ " if (editing and m in added_set) else "  "
                items.append(PickItem(m, f"{mark}{m}"))
            for m in removed:
                items.append(PickItem(f"-{m}", f"- {m}（已移除）", disabled=True))
            footer = [
                PickItem(SUBMIT, "✓ 应用修改" if editing else "✓ 添加路由组"),
                PickItem(CANCEL, "[取消向导]"),
            ]
            picked = await self.app.push_screen_wait(
                PickModal(f"④ 确认路由组（共 {len(state['models'])} 个模型）",
                          items, footer_items=footer,
                          message="点击行可跳回对应步骤编辑")
            )
            if picked is None or picked.value == CANCEL:
                return None
            if picked.value == SUBMIT:
                ok = await self._submit()
                if ok:
                    return 5
                continue
            if picked.value == NAME:
                return 1
            if picked.value == STRATEGY:
                return 3
            if isinstance(picked.value, str):
                return 2

    async def _submit(self) -> bool:
        client = self.app.get_client()
        state = self.state

        def do():
            name = state["name"]
            groups = fresh_groups(client)
            others = [g for g in groups if g.get("group_name") != state.get("orig_name")]
            if any(g.get("group_name") == name for g in others):
                return f"路由组 {name} 已存在"
            dup = set()
            for g in others:
                dup |= set(g.get("models") or []) & set(state["models"])
            if dup:
                owners = sorted({
                    g.get("group_name") for g in others
                    if set(g.get("models") or []) & set(state["models"])
                })
                return f"{', '.join(sorted(dup))} 已属于其他路由组（{', '.join(owners)}）"
            entry = {
                "group_name": name,
                "models": list(state["models"]),
                "routing_strategy": state["strategy"],
            }
            if state.get("orig_name") and state.get("orig_args"):
                entry["routing_strategy_args"] = state["orig_args"]
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
            try:
                client.update_router_settings({"routing_groups": new_groups})
            except LiteLLMError as e:
                return str(e)
            return None

        self.app.status("正在写入路由组…")
        try:
            error = await asyncio.to_thread(do)
        except LiteLLMError as e:
            error = str(e)
        if error:
            self.app.notify_err(f"写入失败: {error}")
            return False
        verb = "已更新" if self.editing else "已添加"
        self.app.notify_ok(f"路由组{verb}: {state['name']}（{len(state['models'])} 个模型, {state['strategy']}）")
        return True
