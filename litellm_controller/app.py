"""Textual 应用入口：全局状态（config/client/cost map）、状态栏与首启配置向导。"""
import asyncio
from pathlib import Path

from rich.spinner import SPINNERS
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static

from .client import LiteLLMClient
from .config import config_exists, load_config
from .widgets import ProgressCover

_DOTS = SPINNERS["dots"]


class StatusBar(Static):
    """底部状态栏：展示最近一次操作结果/进行中提示；spinner=True 时显示点阵转圈。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._text = ""
        self._busy = False
        self._frame = 0
        self._timer = None

    def render(self) -> Text:
        if self._busy:
            glyph = _DOTS["frames"][self._frame % len(_DOTS["frames"])]
            return Text.assemble((glyph, "bold"), " ", self._text)
        return Text(self._text)

    def set_status(self, text: str) -> None:
        self._busy = False
        self._text = text
        self._stop_timer()
        self.refresh()

    def set_spinner(self, text: str) -> None:
        self._busy = True
        self._text = text
        self._ensure_timer()
        self.refresh()

    def _ensure_timer(self) -> None:
        if self._timer is None and self.is_mounted:
            self._timer = self.set_interval(_DOTS["interval"] / 1000, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame += 1
        self.refresh()

    def on_mount(self) -> None:
        self._ensure_timer()


class LiteLLMControllerApp(App):
    """LiteLLM 管理 TUI。"""

    CSS_PATH = Path(__file__).parent / "app.tcss"
    TITLE = "LiteLLM Controller"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.config: dict | None = None
        self._client: LiteLLMClient | None = None
        self._cost_map: dict | None = None

    # ------------------------------------------------------------ 全局资源

    def get_client(self) -> LiteLLMClient:
        if self._client is None:
            lit = self.config["litellm"]
            self._client = LiteLLMClient(lit["endpoint"], lit["key"])
        return self._client

    def invalidate_client(self) -> None:
        """连接参数变化后重建 client 并清空 cost map 缓存。"""
        self._client = None
        self._cost_map = None

    async def get_cost_map(self) -> dict:
        """LiteLLM 内置模型 cost map（会话级缓存；失败返回 {}）。"""
        if self._cost_map is None:
            try:
                self._cost_map = await asyncio.to_thread(self.get_client().fetch_model_cost_map)
            except Exception as e:
                self.status(f"获取内置模型列表失败: {e}", "err")
                self._cost_map = {}
        return self._cost_map

    def reload_config(self) -> None:
        self.config = load_config()
        self.invalidate_client()

    # ------------------------------------------------------------ 状态栏 / 提示

    def compose(self) -> ComposeResult:
        yield StatusBar("", id="status")

    def get_loading_widget(self) -> Widget:
        """全局默认：任意控件 .loading=True 时用 indeterminate 进度条覆盖其区域。"""
        return ProgressCover()

    def status(self, message: str, kind: str = "") -> None:
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:  # noqa: BLE001
            return
        bar.update_classes(
            {"ok": kind == "ok", "warn": kind == "warn", "err": kind == "err"}
        )
        bar.set_status(message)

    def spinner(self, on: bool, message: str = "加载中…") -> None:
        """状态栏点阵 spinner（无遮罩、不阻断输入）：适合轻量、即时的后台动作。"""
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:  # noqa: BLE001
            return
        bar.update_classes({})
        if on:
            bar.set_spinner(message)
        else:
            bar.set_status("")

    def notify_ok(self, message: str) -> None:
        self.status(message, "ok")
        self.notify(message, severity="success", timeout=4)

    def notify_err(self, message: str) -> None:
        self.status(message, "err")
        self.notify(message, severity="error", timeout=8)

    def notify_warn(self, message: str) -> None:
        self.status(message, "warn")
        self.notify(message, severity="warning", timeout=5)

    # ------------------------------------------------------------ 启动

    def on_mount(self) -> None:
        self._bootstrap()

    @work(exclusive=True)
    async def _bootstrap(self) -> None:
        from .screens.home import HomeScreen
        from .screens.setup import SetupWizardScreen

        err = None
        try:
            self.config = load_config()
        except Exception as e:
            err = str(e)

        if err is not None:
            self.status("配置文件缺失或损坏，正在打开配置向导…", "warn")
            saved = await self.push_screen_wait(SetupWizardScreen(reason=err, config_missing=not config_exists()))
            if not saved:
                self.exit()
                return
            try:
                self.reload_config()
            except Exception as e2:
                self.notify_err(f"配置仍无效: {e2}")
                self.exit()
                return
        self.push_screen(HomeScreen())
