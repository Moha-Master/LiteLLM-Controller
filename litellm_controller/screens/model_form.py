"""合一型模型表单页面：支持模型添加（批量）、只读查看、就地编辑。

采用全新的单窗口一体化布局，替代旧的多步向导。
"""
import asyncio
import copy
import json

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    RadioButton,
    Select,
    Static,
    Switch,
    TextArea,
)

from ..client import LiteLLMError
from ..config import cost_map_providers
from ..modeldata import credential_display, fmt_cost
from ..upstreams import fetch_upstream_models
from ..widgets import ConfirmModal, busy


class ModelMappingRow(Static):
    """单条模型映射行：勾选框/单选框 + Model Name + Public Name + 删除按钮。"""

    def __init__(
        self,
        model_name: str = "",
        public_name: str = "",
        checked: bool = True,
        is_radio: bool = False,
        is_existing: bool = False,
    ):
        super().__init__(classes="existing-row" if is_existing else "")
        self.model_name_init = model_name
        self.public_name_init = public_name
        self.checked_init = checked
        self.is_radio = is_radio
        self.is_existing = is_existing

    def compose(self) -> ComposeResult:
        if self.is_radio:
            yield RadioButton(value=self.checked_init, classes="row-check")
        else:
            yield Checkbox(value=self.checked_init, classes="row-check")
        yield Input(self.model_name_init, placeholder="Model Name (LiteLLM Name)", compact=True,
                    classes="row-model-name")
        yield Input(self.public_name_init, placeholder="Public Name", compact=True, classes="row-public-name")
        yield Button("✕", variant="error", classes="row-delete")


class ManualAddRow(Static):
    """始终位于第一行的手动添加行。"""

    def compose(self) -> ComposeResult:
        yield Button("[+ 手动添加模型映射条目]", id="row-manual-add", variant="success")


class ModelFormScreen(ModalScreen[bool]):
    """一体化模型管理页面：Add / View / Edit 模式合一。"""

    DEFAULT_CSS = """
    ModelFormScreen {
        align: center middle;
    }
    ModelFormScreen > .modal-box {
        width: 104;
        height: 90%;
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
    #mapping-section {
        border: round $secondary;
        padding: 0 1;
        height: 22;
        margin-bottom: 1;
        layout: vertical;
    }
    .mapping-actions {
        layout: horizontal;
        height: auto;
        margin-bottom: 0;
    }
    .mapping-actions > Button {
        width: 1fr;
    }
    #btn-fetch-litellm {
        margin-right: 1;
    }
    #mapping-list {
        height: 1fr;
        border: round $surface;
        padding: 0 1;
    }
    #mapping-search {
        margin-bottom: 0;
    }
    ModelMappingRow {
        layout: horizontal;
        height: auto;
        align: left middle;
        margin-bottom: 1;
    }
    ModelMappingRow.existing-row > Input {
        color: $warning;
    }
    ModelMappingRow > .row-check {
        width: auto;
        margin-right: 2;
    }
    ModelMappingRow > .row-model-name {
        width: 1fr;
        margin-right: 1;
    }
    ModelMappingRow > .row-public-name {
        width: 1fr;
        margin-right: 1;
    }
    ModelMappingRow > .row-delete {
        width: auto;
    }
    ManualAddRow {
        height: auto;
        margin-bottom: 1;
        align: center middle;
    }
    ManualAddRow > Button {
        width: 100%;
        text-align: center;
    }
    .pricing-row {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
    }
    .pricing-row > Input {
        width: 1fr;
        margin-right: 1;
    }
    .order-weight-row {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
    }
    .order-weight-row > Input {
        width: 1fr;
        margin-right: 1;
    }
    #adv-params, #adv-info {
        height: 5;
        margin-bottom: 1;
        border: round $surface;
    }
    .adv-label {
        margin-top: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    """

    BINDINGS = [Binding("escape,ctrl+c", "back", "返回", show=False)]

    def __init__(self, mode: str = "add", model_data: dict | None = None):
        """
        mode: "add" | "view" | "edit"
        model_data: 选中模型的数据字典 (仅用于 view / edit)
        """
        super().__init__()
        self.mode = mode
        self.model_data = model_data or {}
        self._cost_map = {}
        self._credentials = []
        self._existing_models: set[str] = set()

    def compose(self) -> ComposeResult:
        title_map = {
            "add": "添加模型（批量映射）",
            "view": "查看模型详情",
            "edit": "修改模型配置",
        }
        with Vertical(classes="modal-box"):
            yield Static(Text(title_map[self.mode], style="bold"), classes="modal-title")
            with VerticalScroll(id="form-body"):
                # Enable/Disable switch for View/Edit modes
                if self.mode in ("view", "edit"):
                    blocked = bool((self.model_data.get("model_info") or {}).get("blocked", False))
                    with Horizontal(classes="form-row"):
                        yield Static("状态:", classes="form-label")
                        yield Switch(value=not blocked, id="frm-status-switch")
                        yield Static(" (启用/禁用模型在代理上的服务)", classes="form-input")

                # Provider Dropdown
                with Horizontal(classes="form-row"):
                    yield Static("Provider:", classes="form-label")
                    yield Select([], prompt="选择 Provider...", compact=True, id="frm-provider", classes="form-select")

                # Model Mapping Section
                with Vertical(id="mapping-section"):
                    # Top Get Buttons
                    with Horizontal(classes="mapping-actions", id="act-row"):
                        yield Button("从 LiteLLM 获取", id="btn-fetch-litellm", variant="primary")
                        yield Button("从 Upstream 获取", id="btn-fetch-upstream", variant="primary")

                    with VerticalScroll(id="mapping-list"):
                        yield Input(placeholder="搜索/过滤已添加的模型映射...", compact=True, id="mapping-search")
                        if self.mode == "add":
                            yield ManualAddRow()
                        # Mapping rows dynamically populated on mount/fetch

                # Credential Dropdown
                with Horizontal(classes="form-row"):
                    yield Static("Credential:", classes="form-label")
                    yield Select([], prompt="[不绑定 Credential]", compact=True, id="frm-credential",
                                 classes="form-select")

                # Advanced Options Collapsible
                with Collapsible(title="展开高级选项 (价格 / 权重 / 自定义JSON)", id="adv-opts"):
                    yield Static("自定义价格 ($/1M tokens, 留空表示自动继承):", classes="adv-label")
                    with Horizontal(classes="pricing-row"):
                        yield Input(placeholder="Input Cost", compact=True, id="adv-in-cost")
                        yield Input(placeholder="Output Cost", compact=True, id="adv-out-cost")
                        yield Input(placeholder="Cache Read Cost", compact=True, id="adv-cache-read")
                        yield Input(placeholder="Cache Write Cost", compact=True, id="adv-cache-write")
                    with Horizontal(classes="order-weight-row"):
                        yield Input(placeholder="order (数字, 越小越优先)", compact=True, id="adv-order")
                        yield Input(placeholder="weight (数字, 路由权重)", compact=True, id="adv-weight")
                    yield Static("自定义 LiteLLM Params (JSON 格式):", classes="adv-label")
                    yield TextArea("{}", id="adv-params")
                    yield Static("自定义 Model Info (JSON 格式):", classes="adv-label")
                    yield TextArea("{}", id="adv-info")

            # Bottom Controls（标准顺序：取消在左、主操作、危险最右）
            with Horizontal(classes="btn-row", id="bottom-controls"):
                if self.mode == "add":
                    yield Button("取消", id="btn-cancel", variant="default")
                    yield Button("保存并批量添加", id="btn-save", variant="primary")
                elif self.mode == "view":
                    yield Button("返回列表", id="btn-back", variant="default")
                    yield Button("切换为编辑模式", id="btn-switch-edit", variant="primary")
                elif self.mode == "edit":
                    yield Button("取消", id="btn-cancel", variant="default")
                    yield Button("保存修改", id="btn-save", variant="primary")
                    yield Button("删除此模型", id="btn-delete", variant="error")
                elif self.mode == "view":
                    yield Button("切换为编辑模式", id="btn-switch-edit", variant="primary")
                    yield Button("返回列表", id="btn-back")
                elif self.mode == "edit":
                    yield Button("保存修改", id="btn-save", variant="success")
                    yield Button("删除此模型", id="btn-delete", variant="error")
                    yield Button("取消", id="btn-cancel")

    async def on_mount(self) -> None:
        self.provider_select = self.query_one("#frm-provider", Select)
        self.credential_select = self.query_one("#frm-credential", Select)

        # 1. 载入缓存
        self._cost_map = await self.app.get_cost_map()
        # Provider 选项 = cost map 内置 provider ∪ 已配置 Upstream 绑定的 provider
        provider_set = set(cost_map_providers(self._cost_map))
        for u in self.app.config.get("upstreams") or []:
            bound = (u.get("provider") or "").strip()
            if bound:
                provider_set.add(bound)
        providers = sorted(provider_set)
        cur_params = self.model_data.get("litellm_params") or {}
        cur_prov = cur_params.get("custom_llm_provider") or ""
        if cur_prov and cur_prov not in providers:
            providers = [cur_prov] + providers
        self.provider_select.set_options([(p, p) for p in providers])

        client = self.app.get_client()
        try:
            self._credentials = await asyncio.to_thread(client.list_credentials)
        except Exception:
            self._credentials = []

        try:
            existing_models = await asyncio.to_thread(client.list_models)
            self._existing_models = set()
            for m in existing_models:
                name = m.get("model_name")
                if name:
                    self._existing_models.add(str(name).strip())
                lp = m.get("litellm_params") or {}
                if lp.get("model"):
                    self._existing_models.add(str(lp["model"]).strip())
        except Exception:
            self._existing_models = set()

        cred_rows = credential_display(self._credentials)
        cred_opts = [(f"{c['name']} ({c['provider']})", c['name']) for c in cred_rows]
        cur_cred = cur_params.get("litellm_credential_name") or ""
        if cur_cred and cur_cred not in {c["name"] for c in cred_rows}:
            cred_opts.append((cur_cred, cur_cred))
        self.credential_select.set_options(cred_opts)

        # 2. 根据模式填充数据
        #    add 模式：表格初始为空，由「手动添加」行或获取按钮填充
        if self.mode in ("view", "edit"):
            await self._fill_existing_data()

        if self.mode == "view":
            self._set_read_only(True)

    def action_back(self) -> None:
        self.dismiss(False)

    # ------------------------------------------------------------ 数据填充与状态

    async def _fill_existing_data(self) -> None:
        params = self.model_data.get("litellm_params") or {}
        info = self.model_data.get("model_info") or {}

        # Fill Provider
        prov = params.get("custom_llm_provider") or ""
        if prov:
            self.provider_select.value = prov

        # Fill Mapping
        model_name = params.get("model") or ""
        public_name = self.model_data.get("model_name") or ""
        self._add_mapping_row(model_name, public_name, checked=True, is_radio=True)

        # Fill Credential
        cred = params.get("litellm_credential_name") or ""
        if cred:
            self.credential_select.value = cred

        # Fill Advanced
        self.query_one("#adv-in-cost", Input).value = fmt_cost(info.get("input_cost_per_token"))
        self.query_one("#adv-out-cost", Input).value = fmt_cost(info.get("output_cost_per_token"))
        self.query_one("#adv-cache-read", Input).value = fmt_cost(info.get("cache_read_input_token_cost"))
        self.query_one("#adv-cache-write", Input).value = fmt_cost(info.get("cache_creation_input_token_cost"))
        self.query_one("#adv-order", Input).value = str(info.get("order") or "")
        self.query_one("#adv-weight", Input).value = str(info.get("weight") or "")

        # Exclude controlled fields to build JSON
        params_copy = copy.deepcopy(params)
        for k in ("model", "custom_llm_provider", "litellm_credential_name"):
            params_copy.pop(k, None)
        self.query_one("#adv-params", TextArea).text = json.dumps(params_copy, ensure_ascii=False, indent=2)

        info_copy = copy.deepcopy(info)
        for k in ("id", "blocked", "input_cost_per_token", "output_cost_per_token",
                  "cache_read_input_token_cost", "cache_creation_input_token_cost", "order", "weight"):
            info_copy.pop(k, None)
        self.query_one("#adv-info", TextArea).text = json.dumps(info_copy, ensure_ascii=False, indent=2)

    def _set_read_only(self, read_only: bool) -> None:
        """开启或关闭只读。"""
        self.provider_select.disabled = read_only
        self.credential_select.disabled = read_only

        # Mapping section
        self.query_one("#btn-fetch-litellm", Button).disabled = read_only
        self.query_one("#btn-fetch-upstream", Button).disabled = read_only
        if self.mode == "add":
            self.query_one(ManualAddRow).disabled = read_only

        for row in self.query(ModelMappingRow):
            row.disabled = read_only

        # Advanced
        self.query_one("#adv-in-cost", Input).disabled = read_only
        self.query_one("#adv-out-cost", Input).disabled = read_only
        self.query_one("#adv-cache-read", Input).disabled = read_only
        self.query_one("#adv-cache-write", Input).disabled = read_only
        self.query_one("#adv-order", Input).disabled = read_only
        self.query_one("#adv-weight", Input).disabled = read_only
        self.query_one("#adv-params", TextArea).disabled = read_only
        self.query_one("#adv-info", TextArea).disabled = read_only

    # ------------------------------------------------------------ 增删 Mapping 行

    def _add_mapping_row(self, model_name: str = "", public_name: str = "", checked: bool = True, is_radio: bool = False) -> None:
        is_existing = False
        if self.mode == "add":
            m_cleaned = model_name.strip()
            p_cleaned = public_name.strip()
            if m_cleaned and m_cleaned in self._existing_models:
                is_existing = True
            elif p_cleaned and p_cleaned in self._existing_models:
                is_existing = True

        lst = self.query_one("#mapping-list")
        row = ModelMappingRow(model_name, public_name, checked, is_radio, is_existing=is_existing)
        lst.mount(row)

    @on(Input.Changed, ".row-model-name, .row-public-name")
    def _on_mapping_input_changed(self, event: Input.Changed) -> None:
        if self.mode != "add":
            return
        row = event.input.parent
        if isinstance(row, ModelMappingRow):
            m_val = row.query_one(".row-model-name", Input).value.strip()
            p_val = row.query_one(".row-public-name", Input).value.strip()
            is_exist = False
            if self._existing_models:
                if m_val and m_val in self._existing_models:
                    is_exist = True
                elif p_val and p_val in self._existing_models:
                    is_exist = True
            row.set_class(is_exist, "existing-row")

    @on(Button.Pressed, "#row-manual-add")
    def _manual_add_row(self) -> None:
        lst = self.query_one("#mapping-list")
        row = ModelMappingRow(checked=True)
        try:
            manual_add = self.query_one(ManualAddRow)
            lst.mount(row, after=manual_add)
        except Exception:
            lst.mount(row)

        try:
            search_input = self.query_one("#mapping-search", Input)
            if search_input.value:
                search_input.value = ""
        except Exception:
            pass

    @on(Input.Changed, "#mapping-search")
    def _on_mapping_search(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        for row in self.query(ModelMappingRow):
            m_name = row.query_one(".row-model-name", Input).value.lower()
            p_name = row.query_one(".row-public-name", Input).value.lower()
            row.display = (not q) or (q in m_name or q in p_name)

    @on(Button.Pressed, ".row-delete")
    def _delete_row(self, event: Button.Pressed) -> None:
        row = event.button.parent
        if not isinstance(row, ModelMappingRow):
            return
        if self.mode == "add":
            row.remove()
        else:
            self.app.notify_warn("编辑模式下必须保留该模型映射行")

    @on(RadioButton.Changed, ".row-check")
    def _on_radio_changed(self, event: RadioButton.Changed) -> None:
        """单选逻辑：一个被选中时，取消其它行的选中。"""
        if self.mode != "add" and event.radio_button.value:
            for row in self.query(ModelMappingRow):
                btn = row.query_one(".row-check", RadioButton)
                if btn != event.radio_button:
                    btn.value = False

    # ------------------------------------------------------------ 详情页状态开关

    @on(Switch.Changed, "#frm-status-switch")
    def _on_status_switch(self, event: Switch.Changed) -> None:
        """查看模式下，开关即时启用/禁用该模型。"""
        if self.mode != "view":
            return
        mid = (self.model_data.get("model_info") or {}).get("id")
        if not mid:
            self.app.notify_warn("该模型没有数据库 ID，无法切换状态")
            return
        self._do_toggle_status(str(mid), not event.value)

    @work(exclusive=True)
    async def _do_toggle_status(self, mid: str, blocked: bool) -> None:
        client = self.app.get_client()
        action = "禁用" if blocked else "启用"
        try:
            await asyncio.to_thread(lambda: client.update_model(mid, blocked=blocked))
            self.model_data.setdefault("model_info", {})["blocked"] = blocked
            self.app.notify_ok(f"模型已{action}")
        except LiteLLMError as e:
            self.app.notify_err(f"{action}失败: {e}")

    # ------------------------------------------------------------ 获取模型数据

    @on(Button.Pressed, "#btn-fetch-litellm")
    def _on_fetch_litellm(self) -> None:
        prov = self.provider_select.value
        if not prov or prov is Select.NULL:
            self.app.notify_warn("请先选择 Provider")
            return

        # 找出该 provider 对应的所有 key
        fetched = []
        for key, meta in self._cost_map.items():
            lp = meta.get("litellm_provider") or meta.get("custom_llm_provider") or ""
            if lp.lower() == str(prov).lower():
                fetched.append(key)

        fetched = sorted(set(fetched))
        if not fetched:
            self.app.notify_warn(f"在 LiteLLM 价格表中未匹配到 Provider 为 {prov} 的模型")
            return

        self._merge_fetched_models(fetched)

    @on(Button.Pressed, "#btn-fetch-upstream")
    def _on_fetch_upstream(self) -> None:
        prov = self.provider_select.value
        if not prov or prov is Select.NULL:
            self.app.notify_warn("请先选择 Provider")
            return

        upstreams = self.app.config.get("upstreams") or []
        target = str(prov)
        # 优先按绑定的 provider 字段匹配；旧配置未设 provider 时回退按协议类型匹配
        matches = [u for u in upstreams if (u.get("provider") or "").strip() == target]
        if not matches:
            matches = [u for u in upstreams if (u.get("type") or "").strip() == target]
        if not matches:
            bound = sorted({(u.get("provider") or u.get("type") or "?").strip() for u in upstreams})
            self.app.notify_warn(
                f"没有绑定 Provider 为 {prov} 的 Upstream（已配置: {', '.join(bound) or '无'}）"
            )
            return

        self._do_fetch_upstream(matches[0])

    @work(exclusive=True)
    async def _do_fetch_upstream(self, upstream: dict) -> None:
        self.app.status(f"正在从上游 {upstream['name']} 联网获取模型列表…")
        lst = self.query_one("#mapping-list", VerticalScroll)
        lst.loading = True
        try:
            models = await asyncio.to_thread(lambda: fetch_upstream_models(upstream, timeout=15))
            if not models:
                self.app.notify_warn("上游未返回任何模型")
                return
            self._merge_fetched_models(models)
            self.app.notify_ok(f"成功获取并合并 {len(models)} 个上游模型")
        except Exception as e:
            self.app.notify_err(f"从上游获取模型失败: {e}")
        finally:
            lst.loading = False

    def _merge_fetched_models(self, fetched_models: list[str]) -> None:
        """合并拉取结果。

        - add 模式：保留勾选（固定）的条目，替换未勾选的，追加新条目（默认不勾选）。
        - edit 模式：单选语义；用候选列表替换当前行，并尽量保留当前所选项。
        """
        fetched_models = [str(m).strip() for m in fetched_models if str(m).strip()]
        try:
            self.query_one("#mapping-search", Input).value = ""
        except Exception:
            pass

        if self.mode == "add":
            kept: dict[str, str] = {}
            for row in self.query(ModelMappingRow):
                if row.query_one(".row-check", Checkbox).value:
                    m_name = row.query_one(".row-model-name", Input).value.strip()
                    p_name = row.query_one(".row-public-name", Input).value.strip()
                    if m_name:
                        kept[m_name] = p_name
                row.remove()
            for m, p in kept.items():
                self._add_mapping_row(m, p, checked=True)
            for model in fetched_models:
                if model not in kept:
                    self._add_mapping_row(model, model, checked=False)
            return

        # edit 模式：只允许一个
        current = ""
        current_public = ""
        for row in self.query(ModelMappingRow):
            if row.query_one(".row-check", RadioButton).value:
                current = row.query_one(".row-model-name", Input).value.strip()
                current_public = row.query_one(".row-public-name", Input).value.strip()
            row.remove()

        candidates = list(fetched_models)
        if current and current not in candidates:
            candidates.insert(0, current)
        for model in candidates:
            public = current_public if (model == current and current_public) else model
            self._add_mapping_row(model, public, checked=(model == current), is_radio=True)

    # ------------------------------------------------------------ 按钮处理

    @on(Button.Pressed, "#btn-switch-edit")
    def _switch_edit_mode(self) -> None:
        self.mode = "edit"
        self._set_read_only(False)
        # 重新构建底部 controls（标准顺序：取消、保存、危险）
        ctrls = self.query_one("#bottom-controls")
        ctrls.remove_children()
        ctrls.mount(Button("取消", id="btn-cancel", variant="default"))
        ctrls.mount(Button("保存修改", id="btn-save", variant="primary"))
        ctrls.mount(Button("删除此模型", id="btn-delete", variant="error"))

    @on(Button.Pressed, "#btn-cancel")
    @on(Button.Pressed, "#btn-back")
    def _on_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-delete")
    def _on_delete_press(self) -> None:
        name = self.model_data.get("model_name", "?")
        self.app.push_screen(
            ConfirmModal(f"确认删除模型 {name}？此操作不可恢复。", title="删除模型", default_yes=False, yes="确认删除"),
            lambda ok: self._do_delete() if ok else None
        )

    @work(exclusive=True)
    async def _do_delete(self) -> None:
        client = self.app.get_client()
        mid = (self.model_data.get("model_info") or {}).get("id")
        if not mid:
            self.app.notify_warn("缺失模型数据库 ID")
            return
        try:
            await asyncio.to_thread(lambda: client.delete_model(mid))
            self.app.notify_ok("模型已删除")
            self.dismiss(True)
        except Exception as e:
            self.app.notify_err(f"删除失败: {e}")

    @on(Button.Pressed, "#btn-save")
    def _on_save_press(self) -> None:
        # 1. 验证 Provider
        prov = self.provider_select.value
        if not prov or prov is Select.NULL:
            self.app.notify_err("请选择 Provider")
            return

        # 2. 收集模型映射
        mappings = []
        for row in self.query(ModelMappingRow):
            box = row.query_one(".row-check")
            m_name = row.query_one(".row-model-name", Input).value.strip()
            p_name = row.query_one(".row-public-name", Input).value.strip()
            if box.value or self.mode != "add":
                if not m_name or not p_name:
                    self.app.notify_err("勾选的映射条目其模型名称均不能为空")
                    return
                mappings.append((m_name, p_name))

        if not mappings:
            self.app.notify_err("必须勾选/添加至少一个有效的模型映射")
            return

        # 3. 验证高级选项中的 JSON
        try:
            custom_params = json.loads(self.query_one("#adv-params", TextArea).text)
            if not isinstance(custom_params, dict):
                raise ValueError("LiteLLM Params 必须是 JSON 对象 (dict)")
        except Exception as e:
            self.app.notify_err(f"自定义 LiteLLM Params 错误: {e}")
            return

        try:
            custom_info = json.loads(self.query_one("#adv-info", TextArea).text)
            if not isinstance(custom_info, dict):
                raise ValueError("Model Info 必须是 JSON 对象 (dict)")
        except Exception as e:
            self.app.notify_err(f"自定义 Model Info 错误: {e}")
            return

        # 4. 自定义计费
        model_info = copy.deepcopy(custom_info)
        cost_fields = {
            "#adv-in-cost": "input_cost_per_token",
            "#adv-out-cost": "output_cost_per_token",
            "#adv-cache-read": "cache_read_input_token_cost",
            "#adv-cache-write": "cache_creation_input_token_cost",
        }
        for qid, field in cost_fields.items():
            val = self.query_one(qid, Input).value.strip()
            if val:
                try:
                    # 换算 $/1M -> 原始 token 价格
                    model_info[field] = round(float(val) / 1e6, 12)
                except ValueError:
                    self.app.notify_err(f"价格参数格式错误: {val}")
                    return

        # 5. order & weight
        for key, field in (("#adv-order", "order"), ("#adv-weight", "weight")):
            val = self.query_one(key, Input).value.strip()
            if val:
                try:
                    model_info[field] = int(val)
                except ValueError:
                    self.app.notify_err(f"{field} 必须是整数: {val}")
                    return

        # 6. Status Switch (blocked)
        if self.mode in ("view", "edit"):
            sw = self.query_one("#frm-status-switch", Switch)
            model_info["blocked"] = not sw.value

        # 7. Credential
        cred = self.credential_select.value
        cred_str = "" if cred is Select.NULL or not cred else str(cred)

        self._do_save(prov, mappings, cred_str, model_info, custom_params)

    @work(exclusive=True)
    async def _do_save(self, provider: str, mappings: list[tuple[str, str]], credential: str, model_info: dict, custom_params: dict) -> None:
        client = self.app.get_client()
        try:
            async with busy(self.app, "正在保存模型…", mode="dots"):
                if self.mode == "add":
                    # 批量添加
                    count = 0
                    for model_name, public_name in mappings:
                        lp = {
                            "model": model_name,
                            "custom_llm_provider": provider,
                        }
                        if credential:
                            lp["litellm_credential_name"] = credential
                        # 合并自定义 parameters
                        lp.update(custom_params)

                        await asyncio.to_thread(
                            lambda pn=public_name, params=lp: client.create_model(
                                model_name=pn,
                                litellm_params=params,
                                model_info=model_info or None,
                            )
                        )
                        count += 1
                    self.app.notify_ok(f"成功批量导入 {count} 个模型")
                else:
                    # 修改单模型
                    mid = (self.model_data.get("model_info") or {}).get("id")
                    model_name, public_name = mappings[0]
                    lp = {
                        "model": model_name,
                        "custom_llm_provider": provider,
                    }
                    if credential:
                        lp["litellm_credential_name"] = credential
                    lp.update(custom_params)

                    await asyncio.to_thread(lambda: client.update_model(
                        mid,
                        model_name=public_name,
                        litellm_params=lp,
                        model_info=model_info or None,
                    ))
                    self.app.notify_ok(f"模型 {public_name} 修改已保存")

            self.dismiss(True)
        except LiteLLMError as e:
            self.app.notify_err(f"保存失败: {e}")
