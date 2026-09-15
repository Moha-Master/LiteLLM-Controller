"""共享 UI 组件：模糊筛选单/多选、确认、输入、表单、输出查看与表格辅助。

所有模态均以 ModalScreen 泛型返回值 + push_screen_wait 消费；
数据展示统一走 DataTable/SelectionList，不再使用字符串伪表格。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
)

try:  # textual 各版本导出位置不同
    from textual.widgets import Option
except ImportError:  # pragma: no cover
    from textual.widgets._option_list import Option


# ---------------------------------------------------------------- 文案与配色

STYLE_OK = "#4ec96a"
STYLE_ERR = "#e06c75"
STYLE_WARN = "#e5c07b"
STYLE_INFO = "#61afef"
STYLE_DIM = "dim"


def tint(value, style: str) -> Text:
    t = Text(str(value))
    t.stylize(style)
    return t


def colored_text(value: str, *, err: bool = False, warn: bool = False, dim: bool = False) -> Text:
    t = Text(value)
    if err:
        t.stylize(STYLE_ERR)
    elif warn:
        t.stylize(STYLE_WARN)
    elif dim:
        t.stylize("dim")
    return t


HINT_PICK = "输入筛选 · ↑↓ 移动 · 回车 确认 · Esc 取消"
HINT_MULTI = "输入筛选 · 空格 勾选 · ↑↓ 移动 · 确认 提交 · Esc 取消"
HINT_MENU = "↑↓ 移动 · 回车 确认 · Esc 返回"


# ---------------------------------------------------------------- 模糊匹配

def fuzzy_score(needle: str, haystack: str):
    """返回可排序匹配分数（越小越优），不匹配返回 None。"""
    if not needle:
        return (0, 0, 0)
    n, h = needle.lower(), haystack.lower()
    pos = h.find(n)
    if pos >= 0:
        return (0, pos, 0)
    idx = -1
    first = -1
    for ch in n:
        idx = h.find(ch, idx + 1)
        if idx < 0:
            return None
        if first < 0:
            first = idx
    return (1, first, idx - first)


def filter_fuzzy(items: list, needle: str, key: Callable[[Any], str]) -> list:
    """按模糊分数排序过滤；needle 为空时原样返回。"""
    if not needle:
        return list(items)
    scored = []
    for i, it in enumerate(items):
        s = fuzzy_score(needle, key(it))
        if s is not None:
            scored.append((s, i, it))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [it for _, _, it in scored]


# ---------------------------------------------------------------- 选项模型

@dataclass
class PickItem:
    value: Any
    label: str
    disabled: bool = False
    hint: str = ""
    preselected: bool = False

    def display(self) -> str:
        if self.hint:
            sep = "  →  " if self.disabled else "  ·  "
            return f"{self.label}{sep}{self.hint}"
        return self.label


def as_pick_items(values: Iterable, *, label: Callable[[Any], str] = str, **kw) -> list:
    return [PickItem(v, label(v), **kw) for v in values]


# ---------------------------------------------------------------- 确认弹窗

class ConfirmModal(ModalScreen[bool]):
    """通用确认框：回车=确认，Esc/取消=否。"""

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal > .modal-box { width: 76; height: auto; max-height: 90%; }
    ConfirmModal .cf-msg { height: auto; margin-bottom: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, message, title="请确认", *, yes="确认", no="取消", default_yes=True):
        super().__init__()
        self.message = message if isinstance(message, Text) else Text(str(message))
        self.title_text = title
        self.yes_label = yes
        self.no_label = no
        self.default_yes = default_yes

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            yield Static(self.message, classes="cf-msg")
            with Horizontal(classes="btn-row"):
                yes_variant = "primary" if self.default_yes else "error"
                yield Button(self.yes_label, id="yes", variant=yes_variant)
                yield Button(self.no_label, id="no", variant="default")

    def on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------- 输入弹窗

class InputModal(ModalScreen[str | None]):
    """单行输入（validator 返回 False 时提示错误并停留）。留空=保持原值。None=取消。"""

    DEFAULT_CSS = """
    InputModal { align: center middle; }
    InputModal > .modal-box { width: 80; height: auto; }
    InputModal .im-error { color: $error; height: auto; }
    InputModal .im-hint { color: $text-muted; height: auto; }
    InputModal Input { margin-bottom: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(
        self,
        message,
        *,
        title="输入",
        value: str = "",
        password: bool = False,
        validator: Callable[[str], bool] | None = None,
        error: str = "输入不合法",
        hint: str = "",
    ):
        super().__init__()
        self.message = message if isinstance(message, Text) else Text(str(message))
        self.title_text = title
        self.initial = value
        self.password = password
        self.validator = validator
        self.error_text = error
        self.hint_text = hint

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            yield Static(self.message, classes="im-msg")
            yield Input(
                value=self.initial if not self.password else "",
                password=self.password,
                placeholder=self.initial if self.password else "",
                id="im-input",
            )
            if self.hint_text:
                yield Static(self.hint_text, classes="im-hint")
            yield Static("", classes="im-error", id="im-error")

    def on_mount(self) -> None:
        self.query_one("#im-input", Input).focus()

    def _submit(self) -> None:
        inp = self.query_one("#im-input", Input)
        val = inp.value if self.password else inp.value.strip()
        if not val and self.password and self.initial:
            val = self.initial  # 密码留空表示保持原值
        if self.validator and not self.validator(val):
            self.query_one("#im-error", Static).update(Text(self.error_text, style=STYLE_ERR))
            return
        self.dismiss(val)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 单选弹窗（模糊筛选）

class PickModal(ModalScreen[PickItem | None]):
    """模糊筛选单选列表，尾部可带操作项（手动输入/返回等）。"""

    DEFAULT_CSS = """
    PickModal { align: center middle; }
    PickModal > .modal-box { width: 92; height: 80%; }
    PickModal .pk-hint { color: $text-muted; height: 1; }
    PickModal OptionList { height: 1fr; border: round $secondary; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, items: list, *, footer_items: list | None = None, message: str = ""):
        super().__init__()
        self.title_text = title
        self.items = list(items)
        self.footer_items = list(footer_items or [])
        self.message = message
        self._visible: list = []

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            if self.message:
                yield Static(self.message, classes="pk-msg")
            yield Input(placeholder="输入以筛选…", id="pk-search")
            yield OptionList(id="pk-list", markup=False)
            yield Static(HINT_PICK, classes="pk-hint")

    def on_mount(self) -> None:
        self._rebuild("")
        self.query_one("#pk-search", Input).focus()

    def _rebuild(self, needle: str) -> None:
        data = filter_fuzzy(self.items, needle, lambda it: it.display())
        self._visible = data + self.footer_items
        ol = self.query_one("#pk-list", OptionList)
        ol.clear_options()
        ol.add_options([Option(it.display(), disabled=it.disabled) for it in self._visible])
        if self._visible:
            first = next((i for i, it in enumerate(self._visible) if not it.disabled), None)
            if first is not None:
                ol.highlighted = first
        n = sum(1 for it in self.items if not it.disabled)
        ol.border_title = f"{self.title_text} (共 {n})"

    @on(Input.Changed, "#pk-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._rebuild(event.value.strip())

    @on(OptionList.OptionSelected)
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._visible):
            item = self._visible[idx]
            if not item.disabled:
                self.dismiss(item)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 多选弹窗

class MultiPickModal(ModalScreen[list | None]):
    """模糊筛选多选（value 必须为 str）。返回选中的 value 列表；None=取消。"""

    DEFAULT_CSS = """
    MultiPickModal { align: center middle; }
    MultiPickModal > .modal-box { width: 92; height: 85%; }
    MultiPickModal .mp-hint { color: $text-muted; height: 1; }
    MultiPickModal SelectionList { height: 1fr; border: round $secondary; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, items: list, *, message: str = ""):
        super().__init__()
        self.title_text = title
        self.items = list(items)  # PickItem, value=str, disabled=锁定, preselected=预勾选
        self.message = message
        self._checked: set[str] = {it.value for it in self.items if it.preselected}
        self._locked = {it.value for it in self.items if it.disabled}
        self._visible: list = []

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            if self.message:
                yield Static(self.message, classes="mp-msg")
            yield Input(placeholder="输入以筛选…", id="mp-search")
            yield SelectionList(id="mp-list")
            with Horizontal(classes="btn-row"):
                yield Button("确认", id="mp-ok", variant="primary")
                yield Button("取消", id="mp-cancel", variant="default")
            yield Static(HINT_MULTI, classes="mp-hint")

    def on_mount(self) -> None:
        self._rebuild("")
        self._update_count()
        self.query_one("#mp-search", Input).focus()

    def _sync_checked(self) -> None:
        visible = {it.value for it in self._visible if not it.disabled}
        sel = self.query_one("#mp-list", SelectionList)
        self._checked = (self._checked - visible) | set(sel.selected)
        self._checked -= self._locked

    def _rebuild(self, needle: str) -> None:
        self._sync_checked()
        self._visible = filter_fuzzy(self.items, needle, lambda it: it.display())
        sel = self.query_one("#mp-list", SelectionList)
        sel.clear_options()
        sel.add_options([
            (it.display(), it.value, it.value in self._checked)
            for it in self._visible
        ])
        for it in self._visible:
            if it.disabled:
                sel.disable_option(it.value)

    def _update_count(self) -> None:
        self.query_one("#mp-list", SelectionList).border_title = f"已勾选 {len(self._checked)} 项"

    @on(Input.Changed, "#mp-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._rebuild(event.value.strip())

    @on(SelectionList.SelectedChanged)
    def _on_toggle(self, event: SelectionList.SelectedChanged) -> None:
        self._checked = set(event.selection_list.selected) - self._locked
        self._update_count()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mp-ok":
            self._sync_checked()
            order = [it.value for it in self.items if it.value in self._checked]
            self.dismiss(order)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 表单弹窗

@dataclass
class FormField:
    name: str
    label: str
    kind: str = "text"  # text | password | choice | browse | note
    value: str = ""
    options: list = field(default_factory=list)      # choice: list[(label, value)]
    pick_items: list = field(default_factory=list)   # browse: list[PickItem]
    pick_title: str = "选择"
    validator: Callable[[str], bool] | None = None
    error: str = "输入不合法"
    placeholder: str = ""
    hint: str = ""
    allow_blank: bool = False                         # text 留空是否合法


class FormModal(ModalScreen[dict | None]):
    """多字段表单，返回 {name: value}；None=取消。password 留空保持原值。"""

    DEFAULT_CSS = """
    FormModal { align: center middle; }
    FormModal > .modal-box { width: 96; max-height: 92%; }
    FormModal .frm-note { color: $text-muted; height: auto; }
    FormModal .frm-row { height: auto; margin-bottom: 1; }
    FormModal .frm-label { width: 24; padding: 1 1 0 0; text-align: right; text-style: bold; }
    FormModal .frm-field { width: 1fr; }
    FormModal .frm-field Input, FormModal .frm-field Select { width: 1fr; }
    FormModal .frm-err { color: $error; height: 1; }
    FormModal .frm-body { height: 1fr; }
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, fields: list, *, message: str = "", ok_label="保存"):
        super().__init__()
        self.title_text = title
        self.fields = list(fields)
        self.message = message
        self.ok_label = ok_label

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            if self.message:
                yield Static(self.message, classes="frm-note")
            with VerticalScroll(classes="frm-body"):
                for f in self.fields:
                    if f.kind == "note":
                        yield Static(f.label, classes="frm-note")
                        continue
                    with Horizontal(classes="frm-row"):
                        yield Label(f.label, classes="frm-label")
                        with Vertical(classes="frm-field"):
                            if f.kind in ("text", "password"):
                                yield Input(
                                    value=f.value if f.kind == "text" else "",
                                    password=(f.kind == "password"),
                                    placeholder=(f.value if f.kind == "password" else f.placeholder),
                                    id=f"in-{f.name}",
                                )
                            elif f.kind == "browse":
                                with Horizontal(classes="frm-browse-row"):
                                    yield Input(value=f.value, id=f"in-{f.name}", classes="frm-browse-input")
                                    yield Button("选择…", id=f"br-{f.name}", classes="frm-browse", variant="default")
                            elif f.kind == "choice":
                                yield Select(
                                    [(lab, val) for lab, val in f.options],
                                    value=f.value if f.value else Select.NULL,
                                    allow_blank=not f.value,
                                    id=f"se-{f.name}",
                                )
                            if f.hint:
                                yield Static(f.hint, classes="frm-note")
            yield Static("", classes="frm-err", id="frm-error")
            with Horizontal(classes="btn-row"):
                yield Button(self.ok_label, id="frm-ok", variant="primary")
                yield Button("取消", id="frm-cancel", variant="default")

    def on_mount(self) -> None:
        first_input = self.query(Input)
        if first_input:
            first_input.first().focus()

    @on(Button.Pressed, ".frm-browse")
    def _on_browse(self, event: Button.Pressed) -> None:
        name = event.button.id[len("br-"):]
        f = next((x for x in self.fields if x.name == name), None)
        if f is None:
            return
        items = f.pick_items() if callable(f.pick_items) else f.pick_items
        self.app.push_screen(
            PickModal(f.pick_title, items),
            lambda item: self._browse_picked(name, item),
        )

    def _browse_picked(self, name: str, item: PickItem | None) -> None:
        if item is not None:
            self.query_one(f"#in-{name}", Input).value = str(item.value)

    def _collect(self) -> dict | None:
        err_box = self.query_one("#frm-error", Static)
        err_box.update(Text(""))
        out = {}
        for f in self.fields:
            if f.kind == "note":
                continue
            if f.kind == "choice":
                val = self.query_one(f"#se-{f.name}", Select).value
                val = "" if val is Select.NULL else str(val)
            else:
                val = self.query_one(f"#in-{f.name}", Input).value
                if f.kind == "password":
                    if not val and f.value:
                        val = f.value  # 密码留空保持原值
                else:
                    val = val.strip()
            if f.validator and not f.validator(val):
                err_box.update(Text(f"{f.label}: {f.error}", style=STYLE_ERR))
                return None
            out[f.name] = val
        return out

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "frm-ok":
            data = self._collect()
            if data is not None:
                self.dismiss(data)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 输出查看弹窗

class OutputModal(ModalScreen[None]):
    """滚动文本查看弹窗（脚本预览/JSON 详情等），Esc/回车关闭。"""

    DEFAULT_CSS = """
    OutputModal { align: center middle; }
    OutputModal > .modal-box { width: 100; height: 92%; }
    OutputModal .out-body { height: 1fr; border: round $secondary; }
    OutputModal .out-hint { color: $text-muted; height: 1; }
    """

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("enter", "close", show=False),
    ]

    def __init__(self, title: str, content):
        super().__init__()
        self.title_text = title
        self.content = content if isinstance(content, Text) else Text(str(content))

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(self.title_text, style="bold"), classes="modal-title")
            with VerticalScroll(classes="out-body"):
                yield Static(self.content)
            yield Static("Esc / 回车 关闭 · 滚动浏览", classes="out-hint")

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 表格辅助

def make_table(*headers, cursor: str = "row", zebra: bool = True) -> DataTable:
    dt = DataTable(cursor_type=cursor, zebra_stripes=zebra)
    dt.add_columns(*headers)
    return dt


def load_rows(table: DataTable, rows: list) -> DataTable:
    """全量重填表格。每行是可 add_row 展开的单元格序列（str 或 Text）。"""
    table.clear()
    for row in rows:
        table.add_row(*row)
    return table


__all__ = [
    "PickItem", "as_pick_items", "ConfirmModal", "InputModal", "PickModal",
    "MultiPickModal", "FormField", "FormModal", "OutputModal",
    "make_table", "load_rows", "filter_fuzzy", "tint", "colored_text",
    "STYLE_OK", "STYLE_ERR", "STYLE_WARN", "STYLE_INFO",
    "HINT_PICK", "HINT_MENU", "HINT_MULTI",
]
