"""添加模型向导：provider → 模型列表 → Public 映射 → credential → 确认。

沿用旧版状态机语义：步骤返回值即下一步编号；从确认页跳入的步骤完成后回到确认页。
每一步以模态弹窗呈现，向导屏本身只是状态机宿主。
"""
import asyncio

from rich.text import Text
from textual import work
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..client import LiteLLMError
from ..config import cost_map_providers
from ..modeldata import cost_map_models, credential_display
from ..upstreams import UpstreamError, fetch_upstream_models
from ..widgets import (
    InputModal,
    MultiPickModal,
    PickItem,
    PickModal,
)

MANUAL = "__manual__"
CONTINUE = "__continue__"
BACK = "__back__"
CANCEL = "__cancel__"
SUBMIT = "__submit__"


class ModelWizardScreen(ModalScreen[bool]):
    """True = 已提交添加，False = 取消。"""

    DEFAULT_CSS = """
    ModelWizardScreen { align: center middle; }
    ModelWizardScreen > .wizard-info { width: 80; height: auto; }
    """

    def __init__(self):
        super().__init__()
        self.state = {"provider": "", "models": [], "mappings": {}, "credential": ""}
        self._focus_model = None

    def compose(self):
        with Vertical(classes="modal-box wizard-info"):
            yield Static(Text("添加模型向导", style="bold"), classes="modal-title")
            yield Static(
                "步骤：① Provider → ② 模型 → ③ Public 映射 → ④ Credential → ⑤ 确认\n"
                "Esc 取消向导",
                id="wz-hint", classes="page-hint",
            )

    def _update_hint(self) -> None:
        s = self.state
        try:
            self.query_one("#wz-hint", Static).update(
                f"Provider: {s['provider'] or '-'}  ·  模型: {len(s['models'])}  ·  "
                f"Credential: {s['credential'] or '无'}"
            )
        except Exception:
            pass

    def on_mount(self) -> None:
        self._flow()

    # ------------------------------------------------------------ 状态机

    @work(exclusive=True)
    async def _flow(self) -> None:
        jump_back = False
        step = 1
        while True:
            self._update_hint()
            if step == 5:
                nxt = await self._step_review()
                if nxt is None:
                    self.dismiss(False)
                    return
                if nxt >= 6:
                    self.dismiss(True)
                    return
                step = nxt
                jump_back = True
                continue
            if step == 1:
                nxt = await self._step_provider()
            elif step == 2:
                nxt = await self._step_models()
            elif step == 3:
                nxt = await self._step_mappings()
            else:
                nxt = await self._step_credential()
            if nxt is None:
                self.dismiss(False)
                return
            if jump_back:
                # 从确认页跳入的步骤，完成后一律回到确认页
                jump_back = False
                step = 5
                continue
            step = nxt

    # ------------------------------------------------------------ ① Provider

    async def _step_provider(self):
        cost_map = await self.app.get_cost_map()
        providers = cost_map_providers(cost_map)
        if not providers:
            provider = await self.app.push_screen_wait(
                InputModal("请输入 Custom Provider:", title="① Provider", hint="未获取到 LiteLLM 内置 Provider 列表")
            )
            if provider:
                self.state["provider"] = provider
                return 2
            return None
        items = [PickItem(p, p) for p in providers]
        footer = [PickItem(MANUAL, "[手动输入其他 Provider]")]
        picked = await self.app.push_screen_wait(
            PickModal("① 选择 Custom Provider", items, footer_items=footer)
        )
        if picked is None:
            return None
        if picked.value == MANUAL:
            manual = await self.app.push_screen_wait(InputModal("请输入 Provider:", title="① Provider"))
            if not manual:
                return None
            self.state["provider"] = manual
        else:
            self.state["provider"] = str(picked.value)
        return 2

    # ------------------------------------------------------------ ② 模型列表

    async def _step_models(self):
        while True:
            models = self.state["models"]
            preview = "\n".join(f"  {m}" for m in models[:12]) or "  （尚未添加模型）"
            if len(models) > 12:
                preview += f"\n  … 共 {len(models)} 个"
            footer = [
                PickItem("add", "＋ 添加模型"),
                PickItem(CONTINUE, "继续 →"),
                PickItem(BACK, "← 上一步"),
            ]
            picked = await self.app.push_screen_wait(
                PickModal(f"② 模型列表（已添加 {len(models)} 个）", [],
                          footer_items=footer, message=preview)
            )
            if picked is None or picked.value == BACK:
                return 1
            if picked.value == CONTINUE:
                if not models:
                    self.app.notify_warn("请至少添加一个模型")
                    continue
                return 3
            if picked.value == "add":
                self._add_models_stage()
                # 添加后停留在步骤②（可继续添加或点继续）
                continue

    async def _add_models_stage(self) -> int:
        """返回 1 表示用户退回步骤①。"""
        state = self.state
        cost_map = await self.app.get_cost_map()
        built_in = cost_map_models(cost_map, state["provider"])
        config = self.app.config
        upstreams = config.get("upstreams") or []
        bound = next((u for u in upstreams if u.get("provider") == state["provider"]), None)

        items = []
        footer = []
        if built_in:
            items.append(PickItem("builtin", "从 LiteLLM 内置模型列表获取", hint=f"{len(built_in)} 个"))
        up_label = "从配置的 Upstream 获取"
        if bound:
            items.insert(0, PickItem("upstream", up_label, hint=f"已绑定 {bound['name']}"))
        else:
            items.append(PickItem("upstream", up_label, hint="无绑定，手动选择" if upstreams else "未配置 Upstream", disabled=not upstreams))
        items.append(PickItem(MANUAL, "手动输入"))
        footer.append(PickItem(BACK, "[返回]"))

        picked = await self.app.push_screen_wait(
            PickModal("选择模型来源", items, footer_items=footer,
                      message=f"从 {state['provider']} 添加模型:")
        )
        if picked is None or picked.value == BACK:
            return 0
        if picked.value == "builtin":
            return await self._pick_models(sorted(built_in), f"选择 {state['provider']} 的内置模型")
        if picked.value == "upstream":
            return await self._pick_from_upstream(upstreams, bound)
        if picked.value == MANUAL:
            manual = await self.app.push_screen_wait(
                InputModal("请输入 LiteLLM Model Name:", title="手动输入")
            )
            if manual:
                self._append_models([manual])
            return 0

    async def _pick_models(self, candidates: list, title: str) -> int:
        picked = await self.app.push_screen_wait(
            MultiPickModal(
                title,
                [PickItem(m, m) for m in candidates],
            )
        )
        if picked:
            self._append_models(picked)
        return 0

    async def _pick_from_upstream(self, upstreams: list, bound: dict | None) -> int:
        ordered = ([bound] + [u for u in upstreams if u is not bound]) if bound else list(upstreams)
        items = [
            PickItem(str(i), f"{u['name']} ({u['type']})", hint="已绑定当前 provider" if u is bound else "")
            for i, u in enumerate(ordered)
        ]
        picked = await self.app.push_screen_wait(
            PickModal("请选择 Upstream", items,
                      message="正在从所选 Upstream 拉取模型列表:")
        )
        if picked is None:
            return 0
        upstream = upstreams[int(picked.value)]
        self.app.status(f"正在从 [{upstream['name']}] 拉取模型列表…")
        warnings = []
        try:
            models = await asyncio.to_thread(
                lambda: fetch_upstream_models(upstream, log=warnings.append)
            )
        except UpstreamError as e:
            self.app.notify_err(f"拉取失败: {e}")
            return 0
        for w in warnings:
            self.app.notify_warn(w)
        if not models:
            self.app.notify_warn("模型列表为空")
            return 0
        return await self._pick_models(sorted(models), "选择模型:")

    def _append_models(self, new_models: list) -> int:
        state = self.state
        existing = set(state["models"])
        added = 0
        for m in new_models:
            m = str(m).strip()
            if m and m not in existing:
                state["models"].append(m)
                existing.add(m)
                added += 1
        if added:
            self.app.notify_ok(f"已添加 {added} 个模型")
        return added

    # ------------------------------------------------------------ ③ Public 映射

    async def _step_mappings(self):
        state = self.state
        focus = self._focus_model
        self._focus_model = None
        while True:
            models = state["models"]
            if focus in models:
                ordered = [focus] + [m for m in models if m != focus]
                focus = None
            else:
                ordered = models
            items = [
                PickItem(m, f"{state['mappings'].get(m, m)} <<< {m}")
                for m in ordered
            ]
            footer = [
                PickItem(CONTINUE, "继续 →"),
                PickItem(BACK, "← 上一步"),
            ]
            picked = await self.app.push_screen_wait(
                PickModal("③ Public Name 设置", items, footer_items=footer,
                          message="点击模型行可修改其 Public Name:")
            )
            if picked is None or picked.value == BACK:
                return 2
            if picked.value == CONTINUE:
                return 4
            public = await self.app.push_screen_wait(
                InputModal(
                    f"为 [{picked.value}] 输入 Public Name:",
                    title="③ Public Name",
                    value=state["mappings"].get(picked.value, picked.value),
                    hint="留空则直接使用 LiteLLM Name",
                )
            )
            if public is None:
                continue
            state["mappings"][picked.value] = public or picked.value

    # ------------------------------------------------------------ ④ Credential

    async def _step_credential(self):
        client = self.app.get_client()
        try:
            credentials = await asyncio.to_thread(client.list_credentials)
        except LiteLLMError as e:
            self.app.notify_err(f"获取 Credential 列表失败: {e}")
            credentials = []
        rows = credential_display(credentials)
        items = [
            PickItem(c["name"], c["name"], hint=f"{c['provider']} · {c['key']}")
            for c in rows
        ]
        footer = [
            PickItem(MANUAL, "[手动输入]"),
            PickItem(BACK, "← 上一步"),
        ]
        if not items:
            footer.insert(0, PickItem("", "[不绑定 Credential]"))
        picked = await self.app.push_screen_wait(
            PickModal("④ 选择 Credential", items, footer_items=footer)
        )
        if picked is None or picked.value == BACK:
            return 3
        if picked.value == MANUAL:
            manual = await self.app.push_screen_wait(
                InputModal("请输入 Credential 名称:", title="④ Credential",
                           hint="可留空表示不绑定")
            )
            if manual is None:
                return 3
            self.state["credential"] = manual
        else:
            self.state["credential"] = "" if picked.value == "__none__" else str(picked.value)
        return 5

    # ------------------------------------------------------------ ⑤ 确认

    async def _step_review(self):
        state = self.state
        while True:
            items = [
                PickItem(1, f"Provider: {state['provider']}"),
                PickItem(4, f"Credential: {state['credential'] or '无'}"),
            ]
            items.extend(
                PickItem(m, f"{state['mappings'].get(m, m)} <<< {m}")
                for m in state["models"]
            )
            footer = [
                PickItem(SUBMIT, "✓ 添加模型到 LiteLLM"),
                PickItem(CANCEL, "[取消向导]"),
            ]
            picked = await self.app.push_screen_wait(
                PickModal(f"⑤ 确认添加（共 {len(state['models'])} 个，点击行可编辑）",
                          items, footer_items=footer)
            )
            if picked is None or picked.value == CANCEL:
                return None
            if picked.value == SUBMIT:
                await self._submit()
                return 6
            if picked.value in (1, 4):
                return int(picked.value)
            if isinstance(picked.value, str):
                self._focus_model = picked.value
                return 3

    async def _submit(self) -> None:
        state = self.state
        client = self.app.get_client()

        def do() -> tuple[int, int, list]:
            ok, failed, errs = 0, 0, []
            for m in state["models"]:
                public = state["mappings"].get(m, m)
                params = {"model": m, "custom_llm_provider": state["provider"]}
                if state["credential"]:
                    params["litellm_credential_name"] = state["credential"]
                try:
                    client.create_model(public, params)
                    ok += 1
                except LiteLLMError as e:
                    failed += 1
                    errs.append(f"{public}: {e}")
            return ok, failed, errs

        self.app.status("正在添加模型…")
        ok, failed, errs = await asyncio.to_thread(do)
        if failed:
            self.app.notify_warn(f"完成: 成功 {ok} 个, 失败 {failed} 个。{errs[0] if errs else ''}")
        else:
            self.app.notify_ok(f"已添加 {ok} 个模型")
