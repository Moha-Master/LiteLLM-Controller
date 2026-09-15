"""首启/重置配置向导：LiteLLM 连接 → Upstream 循环添加 → 概览确认。

复用件 UpstreamFormModal 同时供设置页的 Upstream 增/编辑使用。
"""
import asyncio

from rich.text import Text
from textual import work
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..client import LiteLLMClient
from ..config import (
    SUPPORTED_UPSTREAM_TYPES,
    guess_provider_from_name,
    save_config,
)
from ..upstreams import UPSTREAM_TYPES
from ..widgets import ConfirmModal, FormField, FormModal, PickItem


def mask_key(key: str) -> str:
    if not key:
        return "(空)"
    if len(key) <= 10:
        return "****"
    return f"{key[:6]}…{key[-4:]}"


def fetch_known_providers_for(litellm: dict) -> list:
    """同步拉取已知 provider（供 to_thread 使用）。"""
    try:
        client = LiteLLMClient(litellm["endpoint"], litellm["key"], timeout=10)
        cost_map = client.fetch_model_cost_map()
    except Exception:
        return []
    from ..config import cost_map_providers
    return cost_map_providers(cost_map)


# ---------------------------------------------------------------- Upstream 表单

class UpstreamFormModal(FormModal):
    """单个 Upstream 的采集表单（known_providers 为空时 provider 退化为手动输入框）。

    编辑场景（existing 含 key）密码留空 = 保持原 key；新建场景 key 必填。
    """

    def __init__(self, known_providers: list, existing: dict | None = None):
        self.known_providers = list(known_providers or [])
        existing = existing or {}
        has_existing_key = bool(existing.get("key"))
        fields = [
            FormField(
                "name", "名称", value=existing.get("name", ""),
                validator=lambda v: bool(v and v.strip()),
                error="名称不能为空",
                hint="仅用于显示，如 anthropic-main",
            ),
            FormField(
                "type", "类型", kind="choice",
                value=existing.get("type") or "openai",
                options=[(UPSTREAM_TYPES[t]["label"], t) for t in SUPPORTED_UPSTREAM_TYPES],
            ),
            FormField(
                "endpoint", "模型列表 URL", value=existing.get("endpoint", ""),
                validator=lambda v: bool(v and v.strip() and v.strip().startswith(("http://", "https://"))),
                error="请输入完整的 http(s) URL",
                hint="填写完整的模型列表 API URL，不做任何拼接",
            ),
            FormField(
                "key", "Key", kind="password", value=existing.get("key", ""),
                validator=(lambda v: True) if has_existing_key else (lambda v: bool(v and v.strip())),
                error="key 不能为空",
                hint="留空保持原 Key" if has_existing_key else "",
            ),
        ]
        if self.known_providers:
            fields.append(FormField(
                "provider", "绑定 Provider", kind="browse",
                value=existing.get("provider", ""),
                pick_items=lambda: self._provider_items(self.query_one("#in-name", Input).value),
                pick_title="选择绑定的 LiteLLM Provider",
                hint="可留空（不绑定）；点击 选择… 从列表挑选",
            ))
        else:
            fields.append(FormField(
                "provider", "绑定 Provider", value=existing.get("provider", ""),
                hint="留空不绑定；绑定后可在添加模型流程与元数据管理中自动推荐此 Upstream",
            ))
        super().__init__(
            "编辑 Upstream" if existing else "添加 Upstream",
            fields,
        )

    def _provider_items(self, name_value: str) -> list:
        preselect = guess_provider_from_name(name_value, self.known_providers)
        providers = self.known_providers
        if preselect and preselect in providers:
            providers = [preselect] + [p for p in providers if p != preselect]
        items = [
            PickItem(
                p, p,
                hint="按名称推荐" if p == preselect else "",
            )
            for p in providers
        ]
        items.append(PickItem("", "[不绑定]"))
        return items


def build_upstream(data: dict, existing: dict | None = None) -> dict:
    """表单结果 → upstream 配置字典（key 留空保持原值）。"""
    upstream = {
        "name": data["name"].strip(),
        "type": data["type"],
        "endpoint": data["endpoint"].strip(),
        "key": data["key"] or (existing or {}).get("key", ""),
    }
    provider = (data.get("provider") or "").strip()
    if provider:
        upstream["provider"] = provider
    return upstream


# ---------------------------------------------------------------- 概览弹窗

class SummaryModal(ModalScreen[str]):
    """最终配置概览：返回 save / add / cancel。"""

    DEFAULT_CSS = """
    SummaryModal { align: center middle; }
    SummaryModal > .modal-box { width: 100; height: 85%; }
    SummaryModal .sum-body { height: 1fr; border: round $secondary; padding: 0 1; }
    """

    def __init__(self, litellm: dict, upstreams: list):
        super().__init__()
        self.litellm = litellm
        self.upstreams = upstreams

    def compose(self):
        lines = Text()
        lines.append("LiteLLM Endpoint: ", style="bold")
        lines.append(self.litellm["endpoint"] + "\n")
        lines.append("LiteLLM Key     : ", style="bold")
        lines.append(mask_key(self.litellm["key"]) + "\n")
        lines.append(f"Upstreams       : {len(self.upstreams)} 个\n", style="bold")
        for i, u in enumerate(self.upstreams, 1):
            tag = f" · provider:{u['provider']}" if u.get("provider") else ""
            lines.append(f"  {i}. {u['name']} ({u['type']}) {u['endpoint']}{tag}\n")
        with Vertical(classes="modal-box"):
            yield Static(Text("最终配置概览", style="bold"), classes="modal-title")
            with VerticalScroll(classes="sum-body"):
                yield Static(lines)
            with Vertical(classes="btn-row"):
                yield Button("保存并进入", id="save", variant="primary")
                yield Button("继续添加 Upstream", id="add", variant="default")
                yield Button("取消配置", id="cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


# ---------------------------------------------------------------- 向导主流程

class SetupWizardScreen(ModalScreen[bool]):
    """首启/重置配置向导。True=已保存，False=取消（App 应退出）。"""

    DEFAULT_CSS = """
    SetupWizardScreen { align: center middle; }
    SetupWizardScreen > .sw-intro { width: 90; height: auto; }
    """

    def __init__(self, reason: str = "", config_missing: bool = True):
        super().__init__()
        self.reason = reason
        self.config_missing = config_missing
        self.litellm: dict = {}
        self.upstreams: list = []
        self.known_providers: list = []

    def compose(self):
        title = "👋 首次运行：配置 LiteLLM 连接" if self.config_missing else "⚠ 配置不可用：重新配置"
        body = self.reason or "尚未创建配置文件，请设置 LiteLLM Proxy 的连接信息与上游 Upstream。"
        with Vertical(classes="modal-box sw-intro"):
            yield Static(Text(title, style="bold"), classes="modal-title")
            yield Static(body, id="sw-msg")
            yield Static("Esc 取消向导并退出程序", classes="page-hint")

    def on_mount(self) -> None:
        self._flow()

    @work(exclusive=True)
    async def _flow(self) -> None:
        # 1. LiteLLM 连接
        fields = [
            FormField(
                "endpoint", "Endpoint",
                value=self.litellm.get("endpoint", ""),
                validator=lambda v: bool(v and v.strip()),
                error="endpoint 不能为空",
                hint="如 https://llm.example.com",
            ),
            FormField(
                "key", "Master Key", kind="password",
                value=self.litellm.get("key", ""),
                validator=lambda v: bool(v and v.strip()),
                error="key 不能为空",
                hint="LiteLLM 管理端 master key",
            ),
        ]
        data = await self.app.push_screen_wait(
            FormModal("① LiteLLM 连接", fields, message="请输入 LiteLLM Proxy 的连接信息")
        )
        if data is None:
            self.dismiss(False)
            return
        self.litellm = {"endpoint": data["endpoint"].strip(), "key": data["key"]}

        # 拉取已知 provider（失败退化为手动输入）
        self.app.status("正在获取 LiteLLM 内置 Provider 列表…")
        self.known_providers = await asyncio.to_thread(fetch_known_providers_for, self.litellm)
        if not self.known_providers:
            self.app.status("未能获取内置 Provider 列表，Provider 绑定将使用手动输入。", "warn")

        # 2. Upstream 循环
        while True:
            self.app.status(f"请配置 Upstream（已添加 {len(self.upstreams)} 个）")
            result = await self.app.push_screen_wait(
                UpstreamFormModal(self.known_providers)
            )
            if result is None:
                self.dismiss(False)
                return
            self.upstreams.append(build_upstream(result))
            more = await self.app.push_screen_wait(
                ConfirmModal("继续添加另一个 Upstream?", title="添加 Upstream", default_yes=False)
            )
            if not more:
                break

        # 3. 概览并保存
        while True:
            choice = await self.app.push_screen_wait(SummaryModal(self.litellm, self.upstreams))
            if choice == "save":
                save_config({"litellm": self.litellm, "upstreams": self.upstreams})
                self.app.notify_ok("配置已保存")
                self.dismiss(True)
                return
            if choice == "add":
                result = await self.app.push_screen_wait(
                    UpstreamFormModal(self.known_providers)
                )
                if result is not None:
                    self.upstreams.append(build_upstream(result))
                continue
            self.dismiss(False)
            return
