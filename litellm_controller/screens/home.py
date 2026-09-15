"""主菜单屏。"""
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, Static
try:
    from textual.widgets import Option
except ImportError:  # pragma: no cover
    from textual.widgets._option_list import Option

from .. import __version__
from ..widgets import HINT_MENU


class HomeScreen(Screen):
    """功能主菜单：模型管理 / 路由管理 / 设置。"""

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("1", "nav('manage_models')", "模型", show=False),
        Binding("2", "nav('manage_routing')", "路由", show=False),
        Binding("3", "nav('settings')", "设置", show=False),
    ]

    ITEMS = [
        ("manage_models", "模型管理", "查看/添加/编辑/禁用/删除模型"),
        ("manage_routing", "路由管理", "路由组增删改与失效模型清理"),
        ("settings", "设置", "LiteLLM 连接与 Upstream 配置"),
    ]

    def compose(self):
        yield Static(Text(f"LiteLLM Controller v{__version__}", style="bold"), id="home-title")
        yield OptionList(id="home-list", markup=False)
        yield Static(HINT_MENU + " · 1/2/3 直达 · q 退出", classes="page-hint")

    def on_mount(self) -> None:
        ol = self.query_one("#home-list", OptionList)
        ol.add_options(
            Option(f"{label}  ·  {desc}") for _, label, desc in self.ITEMS
        )
        ol.highlighted = 0

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

    @on(OptionList.OptionSelected, "#home-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        key = self.ITEMS[event.option_index][0]
        if key:
            self._open(key)

    def action_nav(self, key: str) -> None:
        self._open(key)

    def action_quit(self) -> None:
        self.app.exit()
