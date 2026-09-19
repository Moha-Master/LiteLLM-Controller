"""主菜单屏：艺术字 logo（窄终端自动降级）+ 版本副标题 + 居中菜单 + 底部提示栏。"""
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import OptionList, Static

from .. import __version__
from ..ui import LOGO_WIDTH, home_col_width, logo_text
from ..widgets import HintBar, MenuMarquee


class HomeScreen(Screen):
    """功能主菜单：模型管理 / 路由管理 / 设置 / 退出。"""

    def __init__(self) -> None:
        super().__init__(classes="logo-page")

    BINDINGS = [
        Binding("escape,ctrl+c", "menu('quit')", "退出"),
        Binding("1", "menu('manage_models')", "模型", show=False),
        Binding("2", "menu('manage_routing')", "路由", show=False),
        Binding("3", "menu('settings')", "设置", show=False),
        Binding("4", "menu('quit')", "退出", show=False),
    ]

    ITEMS = [
        ("manage_models", "模型管理", "管理模型与参数"),
        ("manage_routing", "路由管理", "路由组增删改与失效模型清理"),
        ("settings", "设置", "LiteLLM 连接与 Upstream 配置"),
        ("quit", "退出程序", "结束操作并退出"),
    ]

    def compose(self):
        with Vertical(id="home-main"):
            with Vertical(id="home-col"):
                yield Static(logo_text(), id="home-banner")
                yield Static(Text("LiteLLM Controller", style="bold"), id="home-plain")
                yield Static(f"v{__version__}", id="home-version")
                yield OptionList(id="home-list")
        yield HintBar("↑↓ 选择 · 回车 进入 · 1-4 直达 · Esc/Ctrl+C 退出", classes="page-hint")

    def on_mount(self) -> None:
        ol = self.query_one("#home-list", OptionList)
        self._marquee = MenuMarquee(
            self, "home-list", [(label, desc) for _, label, desc in self.ITEMS]
        )
        ol.add_options(self._marquee.placeholder_options())
        ol.highlighted = 0
        ol.focus()
        self._apply_breakpoint()
        self.call_after_refresh(self._marquee.start)

    def on_resize(self, event) -> None:
        self._apply_breakpoint()
        if getattr(self, "_marquee", None) is not None:
            self._marquee.sync()

    def _apply_breakpoint(self) -> None:
        """宽度不足以容纳艺术字时降级为普通标题文本；列宽跟随 logo。"""
        small = self.size.width < LOGO_WIDTH + 6
        self.set_class(small, "-sm")
        self.query_one("#home-col").styles.width = None if small else home_col_width()

    def _open(self, key: str) -> None:
        from .models import ModelListScreen
        from .routing import RoutingListScreen
        from .settings import SettingsScreen

        if key == "manage_models":
            self.app.push_screen(ModelListScreen())
        elif key == "manage_routing":
            self.app.push_screen(RoutingListScreen())
        elif key == "settings":
            self.app.push_screen(SettingsScreen())
        elif key == "quit":
            self.app.exit()

    @on(OptionList.OptionSelected, "#home-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        key = self.ITEMS[event.option_index][0]
        if key:
            self._open(key)

    def action_menu(self, key: str) -> None:
        self._open(key)
