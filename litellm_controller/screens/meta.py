"""模型参数管理界面：构建导出 / 脚本管理 / default.py 可视化编辑器。

设计原则：
- 一窗到底：单个界面内完成一处配置，避免多级问询式弹窗串联。
- 复用实体：ModelMetaFormScreen / TierModal / CustomFieldsModal / PickModal / InputModal 等。
- 版式与键位遵循 AGENTS.md 第一部分（PageScreen / ClickTable / 模态三段式）。
"""
import asyncio
import copy
import json
import re
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Input,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)

try:
    from textual.widgets.option_list import Option
except ImportError:  # pragma: no cover
    from textual.widgets._option_list import Option

from .. import metadata as eng
from ..config import cost_map_providers
from ..ui import PageScreen
from ..widgets import (
    STYLE_ERR,
    STYLE_OK,
    STYLE_WARN,
    ConfirmModal,
    FormField,
    FormModal,
    InputModal,
    PickItem,
    PickModal,
    busy,
    filter_fuzzy,
    fit_table_columns,
    load_rows,
    make_table,
    rcell,
    shorten,
)

# ---------------------------------------------------------------- 主页

class MetaHomeScreen(PageScreen):
    TITLE = "模型参数管理"
    HINT = "↑↓ 移动 · 回车 进入 · 1-3 直达 · Esc/Ctrl+C 返回"

    DEFAULT_CSS = """
    MetaHomeScreen #mh-list {
        height: auto;
        border: none;
        background: transparent;
    }
    """

    ITEMS = [
        ("build", "构建模型参数"),
        ("edit_default", "编辑基本配置 (default.py)"),
        ("scripts", "脚本管理"),
    ]

    BINDINGS = [
        Binding("1", "meta_open('build')", show=False),
        Binding("2", "meta_open('edit_default')", show=False),
        Binding("3", "meta_open('scripts')", show=False),
    ]

    def compose_page(self):
        with Vertical(classes="panel"):
            yield OptionList(id="mh-list", markup=False)

    def on_mount(self) -> None:
        ol = self.query_one("#mh-list", OptionList)
        ol.add_options([Option(label) for _, label in self.ITEMS])
        ol.highlighted = 0
        ol.focus()

    @on(OptionList.OptionSelected, "#mh-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        key = self.ITEMS[event.option_index][0]
        self._open(key)

    def action_meta_open(self, key: str) -> None:
        self._open(key)

    def _open(self, key: str) -> None:
        if key == "build":
            self.app.push_screen(BuildScreen())
        elif key == "edit_default":
            self.app.push_screen(DefaultEditorScreen())
        elif key == "scripts":
            self.app.push_screen(ScriptsScreen())


# ---------------------------------------------------------------- 构建导出

class BuildScreen(PageScreen):
    """构建模型参数：大文本框预览 JSON，小文本框展示构建日志，右上角导出。"""

    TITLE = "构建模型参数"
    HINT = "Ctrl+R 重新构建 · Esc/Ctrl+C 返回"

    DEFAULT_CSS = """
    BuildScreen #build-json { height: 1fr; }
    BuildScreen #build-log { height: 7; margin-top: 1; }
    BuildScreen #build-summary { height: auto; margin-top: 1; padding: 0 1; }
    """

    def __init__(self):
        super().__init__()
        self._data: dict | None = None

    def compose_toolbar(self):
        yield Button("导出 JSON", id="export", disabled=True, variant="primary")

    def compose_page(self):
        yield TextArea("", read_only=True, id="build-json")
        yield TextArea("", read_only=True, id="build-log")
        yield Static(Text("正在构建…", style="dim"), id="build-summary")

    def on_mount(self) -> None:
        self._run()

    def reload_page(self) -> None:
        self._run()

    @work(exclusive=True)
    async def _run(self) -> None:
        client = self.app.get_client()
        mgr = eng.MetadataManager(client, self.app.config)
        try:
            async with busy(self.app, "正在构建模型参数…", mode="dots"):
                data, logs = await asyncio.to_thread(mgr.build)
                stats = await asyncio.to_thread(mgr.get_diff_stats, data)
        except Exception as e:
            self.query_one("#build-log", TextArea).text = f"构建失败: {e}"
            self.query_one("#build-summary", Static).update(Text("构建失败", style=STYLE_ERR))
            self.app.notify_err(f"构建失败: {e}")
            return
        self._data = data
        self.query_one("#build-json", TextArea).text = json.dumps(data, ensure_ascii=False, indent=2)
        self.query_one("#build-log", TextArea).text = "\n".join(logs) if logs else "(无构建日志)"
        if stats["ok"]:
            self.query_one("#build-summary", Static).update(Text(
                f"对比当前 LiteLLM：新增 {stats['added']} · 修改 {stats['changed']} · "
                f"移除 {stats['removed']} · 共计 {stats['total']} 条",
                style="bold",
            ))
        else:
            self.query_one("#build-summary", Static).update(
                Text("无法获取 Proxy 当前参数，未做对比", style=STYLE_WARN)
            )
        self.query_one("#export", Button).disabled = False
        self.app.status(f"构建完成: {len(data)} 条模型参数")

    @on(Button.Pressed, "#export")
    def _export(self) -> None:
        if self._data is None:
            return
        mgr = eng.MetadataManager(self.app.get_client(), self.app.config)
        out_path = mgr.get_output_path()
        self.app.push_screen(
            ConfirmModal(f"确认导出并覆盖到 {out_path}？", title="导出模型参数"),
            lambda ok: self._do_export(ok),
        )

    @work(exclusive=True)
    async def _do_export(self, ok: bool) -> None:
        if not ok or self._data is None:
            return
        mgr = eng.MetadataManager(self.app.get_client(), self.app.config)
        try:
            async with busy(self.app, "正在导出模型参数…", mode="dots"):
                path = await asyncio.to_thread(mgr.export, self._data)
            self.app.notify_ok(f"成功导出至: {path}")
        except Exception as e:
            self.app.notify_err(f"导出失败: {e}")


# ---------------------------------------------------------------- 脚本管理

SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""自定义参数脚本：在 DATA 中填写模型参数，或于 main() 联网生成后输出 JSON。"""
import json
import sys

DATA = {}


def main():
    json.dump(DATA, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

SCRIPT_SORTS = [
    ("prio", "按优先级"),
    ("name", "按名称"),
]

FILE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\.py$")


class ScriptFormModal(FormModal):
    """脚本设置（一窗到底）：名称 / 描述 / 优先级 / 启用开关 / 脚本内容。"""

    DEFAULT_CSS = """
    ScriptFormModal > .modal-box { width: 104; }
    ScriptFormModal .frm-field TextArea { height: 16; }
    """

    def __init__(self, path: Path | None, meta_dir: Path):
        self.path = path
        self.meta_dir = meta_dir
        if path is not None:
            m = eng.parse_script_meta(path)
            fields = [
                FormField("name", "名称", value=m.name),
                FormField("description", "描述", value=m.description),
                FormField("priority", "优先级", value=str(m.priority),
                          validator=lambda v: bool(v and v.strip().isdigit()),
                          error="优先级须为不小于 0 的整数"),
                FormField("enabled", "启用", kind="switch", value="on" if m.enabled else "",
                          hint="禁用后构建流程跳过该脚本"),
                FormField("content", "脚本内容", kind="textarea", value=path.read_text(encoding="utf-8")),
            ]
            title = f"脚本设置 · {path.name}"
        else:
            fields = [
                FormField("file", "文件名", value="custom.py",
                          validator=lambda v: bool(FILE_RE.match((v or "").strip())),
                          error="文件名需以字母开头、.py 结尾，仅限字母数字下划线中划线",
                          hint="保存在参数脚本目录，不可与现有文件重名"),
                FormField("name", "名称", value="自定义参数脚本"),
                FormField("description", "描述", value="新建的参数脚本"),
                FormField("priority", "优先级", value="50",
                          validator=lambda v: bool(v and v.strip().isdigit()),
                          error="优先级须为不小于 0 的整数",
                          hint="数值越小越先执行（后执行者覆盖）"),
                FormField("enabled", "启用", kind="switch", value="on",
                          hint="禁用后构建流程跳过该脚本"),
                FormField("content", "脚本内容", kind="textarea", value=SCRIPT_TEMPLATE),
            ]
            title = "新建脚本"
        super().__init__(title, fields, ok_label="保存")

    def _collect(self):
        data = super()._collect()
        if data is not None and self.path is None:
            target = self.meta_dir / data["file"].strip()
            if target.exists():
                self.show_error(f"文件已存在: {target.name}")
                return None
        return data


class ScriptsScreen(PageScreen):
    """脚本管理：脚本完全来自配置目录，单击行进入统一的「脚本设置」表单。"""

    TITLE = "脚本管理"
    HINT = "↑↓ 移动 · 单击/回车 脚本设置 · / 搜索 · Ctrl+N 新建 · Ctrl+R 刷新 · Esc/Ctrl+C 返回"

    BINDINGS = [
        Binding("/", "focus_search", "搜索", show=False),
        Binding("ctrl+n", "new_script", "新建", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._files: list = []
        self._display: list = []
        self._filter = ""
        self._sort_key = "prio"

    def compose_page(self):
        with Vertical(classes="panel"):
            with Horizontal(classes="filter-row"):
                yield Static("搜索", classes="fl-label")
                yield Input(placeholder="按名称 / 文件名筛选（/ 聚焦）", compact=True, id="sc-search")
                yield Select(
                    [(label, key) for key, label in SCRIPT_SORTS],
                    value="prio", allow_blank=False, compact=True, id="sc-sort",
                )
                yield Static(classes="fill")
                yield Button("＋ 新建脚本", id="sc-add", variant="primary")
        table = make_table("名称", "脚本文件名", "优先级", "状态")
        table.id = "sc-table"
        yield table

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one(DataTable).focus()

    def reload_page(self) -> None:
        self._rebuild()

    def _meta_dir(self) -> Path:
        return eng.MetadataManager(None, self.app.config).meta_dir

    def _rebuild(self, *, after_layout: bool = False) -> None:
        files = sorted(self._meta_dir().glob("*.py"), key=lambda f: f.name)
        files = [f for f in files if not f.name.startswith(".")]
        metas = {f: eng.parse_script_meta(f) for f in files}
        if self._filter:
            files = filter_fuzzy(
                files, self._filter, lambda f: f"{metas[f].name} {f.name}"
            )
        if self._sort_key == "prio":
            files = sorted(files, key=lambda f: (metas[f].priority, f.name))
        else:
            files = sorted(files, key=lambda f: metas[f].name)
        self._files = files
        self._display = files
        table = self.query_one("#sc-table", DataTable)
        vis = fit_table_columns(table, [20, 24, 8, 8], rows=len(files))
        keys = list(table.columns.keys())
        table.columns[keys[2]].label = rcell("优先级", vis[2] - 2)
        table.columns[keys[3]].label = rcell("状态", vis[3] - 2)
        rows = []
        for f in files:
            m = metas[f]
            status = rcell("启用" if m.enabled else "禁用", vis[3] - 2)
            status.stylize(STYLE_OK if m.enabled else "dim")
            rows.append([
                Text(shorten(m.name, vis[0] - 2)),
                Text(shorten(f.name, vis[1] - 2)),
                rcell(f"P={m.priority}", vis[2] - 2),
                status,
            ])
        load_rows(table, rows)
        self.set_subtitle(f"{len(files)} 个脚本")
        if not after_layout:
            self.call_after_refresh(lambda: self._rebuild(after_layout=True))

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self._rebuild()

    # ---------------------------------------------------------- 打开设置 / 新建

    @on(Input.Changed, "#sc-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(Select.Changed, "#sc-sort")
    def _on_sort(self, event: Select.Changed) -> None:
        if event.value and event.value != Select.NULL:
            self._sort_key = str(event.value)
            self._rebuild()

    @on(DataTable.RowSelected, "#sc-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._display):
            self._open_form(self._display[event.cursor_row])

    def _open_form(self, path: Path) -> None:
        self.app.push_screen(
            ScriptFormModal(path, self._meta_dir()),
            lambda data: self._write_script(path, data) if data is not None else None,
        )

    def _write_script(self, target: Path, data: dict) -> None:
        """内容落盘后统一由头部字段接管 CONFIG 块。"""
        target.write_text(data["content"].rstrip() + "\n", encoding="utf-8")
        eng.update_script_meta_fields(
            target,
            name=data["name"].strip(),
            description=data["description"].strip(),
            priority=int(data["priority"].strip()),
            enabled=bool(data["enabled"]),
        )
        self.app.notify_ok(f"{target.name} 已保存")
        self._rebuild()

    @on(Button.Pressed, "#sc-add")
    def _on_add(self) -> None:
        self.action_new_script()

    def action_new_script(self) -> None:
        meta_dir = self._meta_dir()
        self.app.push_screen(
            ScriptFormModal(None, meta_dir),
            lambda data: self._write_script(meta_dir / data["file"].strip(), data) if data is not None else None,
        )

    def action_focus_search(self) -> None:
        self.query_one("#sc-search", Input).focus()


# ---------------------------------------------------------------- default.py 编辑器

class DefaultEditorScreen(PageScreen):
    """编辑基本配置：列表 + 新增/保存/重置，条目进入统一的参数表单窗口。"""

    TITLE = "编辑基本配置"
    SUBTITLE = "default.py"
    HINT = "↑↓ 移动 · 单击/回车 编辑参数 · Ctrl+N 新增 · Ctrl+R 重载 · Esc/Ctrl+C 返回"

    BINDINGS = [
        Binding("ctrl+n", "add_entry", "新增", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._orig: dict = {}
        self._working: dict = {}
        self._keys: list = []
        self._filter = ""

    def compose_page(self):
        with Vertical(classes="panel"):
            with Horizontal(classes="filter-row"):
                yield Static("搜索", classes="fl-label")
                yield Input(placeholder="按模型键筛选", compact=True, id="de-search")
            with Horizontal(classes="filter-row gap-top"):
                yield Button("＋ 新增模型参数", id="add", variant="primary")
                yield Static(classes="fill")
                yield Button("保存", id="save", variant="primary")
                yield Button("重置", id="reset", variant="error")
        table = make_table("Model Key", "配置摘要")
        table.id = "de-table"
        yield table

    def on_mount(self) -> None:
        self._reload()

    def reload_page(self) -> None:
        """Ctrl+R：有未保存修改时先确认再放弃重载。"""
        if self._is_dirty():
            self.app.push_screen(
                ConfirmModal("有未保存的修改，确认放弃并重新载入？", title="重新载入",
                             default_yes=False, yes="放弃"),
                lambda ok: self._reload() if ok else None,
            )
        else:
            self._reload()

    def _default_path(self) -> Path:
        return eng.MetadataManager(None, self.app.config).meta_dir / "default.py"

    @work(exclusive=True)
    async def _reload(self) -> None:
        await self._load()

    async def _load(self) -> None:
        path = self._default_path()
        if not path.exists():
            await asyncio.to_thread(eng.sync_example_scripts)
        data, error = await asyncio.to_thread(eng.read_default_py_data, path)
        if error:
            self.app.notify_err("无法加载 default.py")
            self.app.pop_screen()
            return
        self._orig = data
        self._working = copy.deepcopy(data)
        self._rebuild()

    def _rebuild(self, *, after_layout: bool = False) -> None:
        keys = sorted(self._working.keys())
        if self._filter:
            keys = [k for k in keys if self._filter.lower() in k.lower()]
        table = self.query_one("#de-table", DataTable)
        vis = fit_table_columns(table, [24, 60], rows=len(keys))
        rows = [
            [Text(shorten(k, vis[0] - 2)), Text(shorten(eng.config_summary(self._working[k]), vis[1] - 2), style="dim")]
            for k in keys
        ]
        self._keys = keys
        load_rows(table, rows)
        self.set_subtitle(f"default.py · {len(self._working)} 个模型")
        if not after_layout:
            self.call_after_refresh(lambda: self._rebuild(after_layout=True))

    def on_resize(self, event) -> None:
        if self.is_mounted:
            self._rebuild()

    @on(Input.Changed, "#de-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(DataTable.RowSelected, "#de-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._keys):
            key = self._keys[event.cursor_row]
            self._open_form(key, copy.deepcopy(self._working[key]), "edit")

    @on(Button.Pressed, "#add")
    def _add(self) -> None:
        self.action_add_entry()

    def action_add_entry(self) -> None:
        self._open_form("", {}, "add")

    def _open_form(self, key: str, meta: dict, mode: str) -> None:
        self.app.push_screen(
            ModelMetaFormScreen(key, meta, mode),
            lambda res: self._entry_done(key, res),
        )

    def _entry_done(self, key: str, res) -> None:
        if res is None:
            return
        if res[0] == "delete":
            self._working.pop(key, None)
        elif res[0] == "save":
            _, new_key, new_meta = res
            if new_key != key:
                self._working.pop(key, None)
            self._working[new_key] = new_meta
        self._rebuild()

    # ---------------------------------------------------------- 保存 / 重置 / 取消

    def _is_dirty(self) -> bool:
        return self._working != self._orig

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        old_k, new_k = set(self._orig), set(self._working)
        added = len(new_k - old_k)
        deleted = len(old_k - new_k)
        changed = sum(1 for k in old_k & new_k if self._orig[k] != self._working[k])
        if added == deleted == changed == 0:
            self.app.status("没有需要保存的变更")
            return
        self.app.push_screen(
            ConfirmModal(
                f"变动概览: 新增 {added} / 删除 {deleted} / 修改 {changed}\n保存到 default.py？",
                title="保存 default.py",
            ),
            lambda ok: self._do_save(ok) if ok else None,
        )

    @work(exclusive=True)
    async def _do_save(self, ok: bool) -> None:
        if not ok:
            return
        await asyncio.to_thread(eng.save_default_py, self._default_path(), self._working)
        self._orig = copy.deepcopy(self._working)
        self.app.notify_ok("default.py 已保存")

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        self.app.push_screen(
            ConfirmModal("确认放弃全部改动并重新载入 default.py？", title="重置",
                         default_yes=False, yes="重置"),
            lambda ok: self._do_reset() if ok else None,
        )

    @work(exclusive=True)
    async def _do_reset(self) -> None:
        await self._load()
        self.app.notify_ok("已重置为磁盘上的 default.py")

    def action_page_back(self) -> None:
        """Esc / ◀ 返回：有未保存修改时先确认。"""
        if not self._is_dirty():
            self.app.pop_screen()
            return
        self.app.push_screen(
            ConfirmModal("有未保存的修改，确认放弃并返回？", title="取消", default_yes=False, yes="放弃"),
            lambda ok: self.app.pop_screen() if ok else None,
        )


# ---------------------------------------------------------------- 单模型参数表单（一窗到底）

class ModelMetaFormScreen(ModalScreen[tuple]):
    """统一的模型参数配置窗口：新增 / 编辑复用，返回 ("save", key, meta) / ("delete",) / None。"""

    DEFAULT_CSS = """
    ModelMetaFormScreen { align: center middle; }
    ModelMetaFormScreen > .modal-box { width: 100; height: 90%; }
    ModelMetaFormScreen .form-row { layout: horizontal; height: auto; margin-bottom: 1; align: left middle; }
    ModelMetaFormScreen .form-label { width: 20; height: 1; content-align: left middle; text-style: bold; }
    ModelMetaFormScreen .form-input { width: 1fr; }
    ModelMetaFormScreen .form-inputs { layout: horizontal; height: auto; margin-bottom: 1; }
    ModelMetaFormScreen .form-inputs Input { width: 1fr; margin-right: 1; }
    ModelMetaFormScreen .form-inputs Button { margin-right: 1; }
    ModelMetaFormScreen #mmf-scroll { height: 1fr; padding: 1 2; }
    ModelMetaFormScreen #mmf-features { height: 7; border: round $surface; margin-bottom: 1; }
    ModelMetaFormScreen .section-label { margin-top: 1; margin-bottom: 1; text-style: bold; }
    ModelMetaFormScreen .hint { color: $text-muted; }
    ModelMetaFormScreen #mmf-tier-list { height: auto; max-height: 6; border: round $surface; }
    """

    BINDINGS = [Binding("escape,ctrl+c", "cancel", show=False)]

    MANAGED_FIELDS = (
        "litellm_provider",
        "input_cost_per_token", "output_cost_per_token",
        "cache_read_input_token_cost", "cache_creation_input_token_cost",
        "max_input_tokens", "max_output_tokens", "max_tokens",
        "mode", "rpm", "tpm", "output_cost_per_reasoning_token",
        "deprecation_date", "source",
    )

    COST_FIELDS = (
        ("#mmf-in", "input_cost_per_token"),
        ("#mmf-out", "output_cost_per_token"),
        ("#mmf-cache-read", "cache_read_input_token_cost"),
        ("#mmf-cache-write", "cache_creation_input_token_cost"),
    )

    def __init__(self, key: str, meta: dict, mode: str = "edit"):
        super().__init__()
        self.key = key
        self.meta = copy.deepcopy(meta)
        self.mode = mode
        self._extra_features: list[str] = []

    def compose(self) -> ComposeResult:
        title = "新增模型参数" if self.mode == "add" else f"模型参数: {self.key}"
        with Vertical(classes="modal-box"):
            yield Static(Text(title, style="bold"), classes="modal-title")
            with VerticalScroll(id="mmf-scroll"):
                with Horizontal(classes="form-row"):
                    yield Static("Model Key:", classes="form-label")
                    yield Input(self.key, placeholder="如 deepseek-chat", id="mmf-key", compact=True,
                                classes="form-input", disabled=self.mode != "add")
                with Horizontal(classes="form-row"):
                    yield Static("LiteLLM Provider:", classes="form-label")
                    yield Select([], allow_blank=True, prompt="（不设置）", id="mmf-provider", compact=True,
                                 classes="form-input")
                yield Static("价格 ($/1M tokens，留空不设置)", classes="section-label")
                with Horizontal(classes="form-inputs"):
                    yield Input(placeholder="Input", id="mmf-in", compact=True)
                    yield Input(placeholder="Output", id="mmf-out", compact=True)
                with Horizontal(classes="form-inputs"):
                    yield Input(placeholder="Cache Read", id="mmf-cache-read", compact=True)
                    yield Input(placeholder="Cache Write", id="mmf-cache-write", compact=True)
                yield Static("上下文与类型", classes="section-label")
                with Horizontal(classes="form-inputs"):
                    yield Input(placeholder="Max Input Tokens", id="mmf-max-in", compact=True)
                    yield Input(placeholder="Max Output Tokens", id="mmf-max-out", compact=True)
                with Horizontal(classes="form-row"):
                    yield Static("Mode:", classes="form-label")
                    yield Select([(m, m) for m in eng.MODE_CHOICES], id="mmf-mode", compact=True,
                                 classes="form-input")
                yield Static("特性支持 (勾选 = True)", classes="section-label")
                yield SelectionList(id="mmf-features")
                with Horizontal(classes="form-inputs"):
                    yield Button("＋ 自定义特性字段", id="mmf-add-feature")
                with Collapsible(title="高级字段", id="mmf-adv"):
                    with Horizontal(classes="form-inputs"):
                        yield Input(placeholder="RPM", id="mmf-rpm", compact=True)
                        yield Input(placeholder="TPM", id="mmf-tpm", compact=True)
                    with Horizontal(classes="form-inputs"):
                        yield Input(placeholder="Reasoning Output $/1M", id="mmf-reasoning", compact=True)
                        yield Input(placeholder="Deprecation Date (YYYY-MM-DD)", id="mmf-deprecation", compact=True)
                    with Horizontal(classes="form-inputs"):
                        yield Input(placeholder="来源 source", id="mmf-source", compact=True)
                with Collapsible(title="阶梯定价", id="mmf-tiers"):
                    yield OptionList(id="mmf-tier-list", markup=False)
                    with Horizontal(classes="form-inputs"):
                        yield Button("＋ 新增阶梯", id="mmf-add-tier")
                with Collapsible(title="自定义字段", id="mmf-custom"):
                    yield Static("", id="mmf-custom-summary", classes="hint")
                    with Horizontal(classes="form-inputs"):
                        yield Button("管理自定义字段", id="mmf-edit-custom")
            with Horizontal(classes="btn-row"):
                yield Button("取消", id="mmf-cancel", variant="default")
                yield Button("保存", id="mmf-save", variant="primary")
                if self.mode == "edit":
                    yield Button("删除", id="mmf-delete", variant="error")

    async def on_mount(self) -> None:
        # Provider 选项 = cost map 内置 ∪ 已配置 Upstream 绑定 ∪ 当前值
        provider_set = set(cost_map_providers(await self.app.get_cost_map()))
        for u in self.app.config.get("upstreams") or []:
            bound = (u.get("provider") or "").strip()
            if bound:
                provider_set.add(bound)
        cur_prov = self.meta.get("litellm_provider") or ""
        if cur_prov:
            provider_set.add(cur_prov)
        self.query_one("#mmf-provider", Select).set_options([(p, p) for p in sorted(provider_set)])

        self._fill_fields()
        self._rebuild_features()
        self._rebuild_tiers()
        self._rebuild_custom()

    def _fill_fields(self) -> None:
        m = self.meta
        if m.get("litellm_provider"):
            self.query_one("#mmf-provider", Select).value = m["litellm_provider"]
        for qid, field in self.COST_FIELDS:
            self.query_one(qid, Input).value = eng.cost_m_str(m.get(field))
        max_in = m.get("max_input_tokens") or m.get("max_tokens")
        self.query_one("#mmf-max-in", Input).value = str(max_in) if max_in is not None else ""
        max_out = m.get("max_output_tokens")
        self.query_one("#mmf-max-out", Input).value = str(max_out) if max_out is not None else ""
        self.query_one("#mmf-mode", Select).value = m.get("mode") or "chat"
        self.query_one("#mmf-rpm", Input).value = str(m["rpm"]) if m.get("rpm") is not None else ""
        self.query_one("#mmf-tpm", Input).value = str(m["tpm"]) if m.get("tpm") is not None else ""
        self.query_one("#mmf-reasoning", Input).value = eng.cost_m_str(m.get("output_cost_per_reasoning_token"))
        self.query_one("#mmf-deprecation", Input).value = m.get("deprecation_date") or ""
        self.query_one("#mmf-source", Input).value = m.get("source") or ""

    # ---------------------------------------------------------- 特性

    def _feature_keys(self) -> list[str]:
        keys = list(eng.BUILTIN_FEATURES)
        keys += sorted(k for k in self.meta if k.startswith("supports_") and k not in eng.BUILTIN_FEATURES)
        keys += [k for k in self._extra_features if k not in keys]
        return keys

    def _rebuild_features(self) -> None:
        sel = self.query_one("#mmf-features", SelectionList)
        sel.clear_options()
        sel.add_options([(k, k, self.meta.get(k) is True) for k in self._feature_keys()])

    @on(Button.Pressed, "#mmf-add-feature")
    def _add_feature(self) -> None:
        self.app.push_screen(
            InputModal("输入自定义特性字段（supports_ 前缀会自动添加）:", title="自定义特性",
                       validator=lambda v: bool(v and v.strip()), error="不能为空"),
            lambda v: self._add_feature_value(v),
        )

    def _add_feature_value(self, name: str | None) -> None:
        if not name:
            return
        field = name.strip()
        if not field.startswith("supports_"):
            field = "supports_" + field
        if field in self._feature_keys():
            self.app.notify_warn(f"字段 '{field}' 已存在")
            return
        self._extra_features.append(field)
        self._rebuild_features()

    # ---------------------------------------------------------- 阶梯定价

    def _rebuild_tiers(self) -> None:
        ol = self.query_one("#mmf-tier-list", OptionList)
        ol.clear_options()
        self._tiers = eng.collect_tiers(self.meta)
        if not self._tiers:
            ol.add_options([Option("（暂无阶梯，点击下方「新增阶梯」创建）")])
        else:
            ol.add_options([Option(f"输入 tokens > {t}k 时整单适用（回车管理）") for t in self._tiers])

    @on(OptionList.OptionSelected, "#mmf-tier-list")
    def _on_tier(self, event: OptionList.OptionSelected) -> None:
        if not getattr(self, "_tiers", None):
            return
        t = self._tiers[event.option_index]
        self.app.push_screen(TierModal(self.meta, t), lambda _: self._rebuild_tiers())

    @on(Button.Pressed, "#mmf-add-tier")
    def _add_tier(self) -> None:
        self.app.push_screen(
            InputModal("输入阶梯触发阈值 N（单位 k tokens）:", title="新增阶梯",
                       validator=eng.valid_positive_int, error="请输入正整数",
                       hint="触发键为 input_cost_per_token_above_Nk_tokens（无此项该阶梯不会触发）"),
            lambda v: self._new_tier(v),
        )

    def _new_tier(self, v: str | None) -> None:
        if not v:
            return
        t = int(v)
        self.app.push_screen(TierModal(self.meta, t), lambda _: self._rebuild_tiers())

    # ---------------------------------------------------------- 自定义字段

    def _custom_count(self) -> int:
        return len(self._custom_fields())

    def _custom_fields(self) -> list[str]:
        tier_keys = {
            f"{base}_above_{t}k_tokens"
            for t in eng.collect_tiers(self.meta)
            for base, _ in eng.TIER_FIELDS
        }
        return [
            k for k in self.meta
            if k not in eng.KNOWN_META_FIELDS
            and not k.startswith("supports_")
            and k not in tier_keys
        ]

    def _rebuild_custom(self) -> None:
        self.query_one("#mmf-custom-summary", Static).update(
            Text(f"当前共 {self._custom_count()} 个自定义字段", style="dim")
        )

    @on(Button.Pressed, "#mmf-edit-custom")
    def _edit_custom(self) -> None:
        self.app.push_screen(CustomFieldsModal(self.meta), lambda _: self._rebuild_custom())

    # ---------------------------------------------------------- 保存 / 删除 / 取消

    def _collect(self) -> dict:
        meta = copy.deepcopy(self.meta)
        for field in self.MANAGED_FIELDS:
            meta.pop(field, None)

        prov = self.query_one("#mmf-provider", Select).value
        if prov and prov is not Select.NULL:
            meta["litellm_provider"] = str(prov)

        for qid, field in self.COST_FIELDS:
            val = self.query_one(qid, Input).value.strip()
            if val:
                try:
                    meta[field] = round(float(val) / 1_000_000, 12)
                except ValueError as e:
                    raise ValueError(f"价格字段格式错误: {val}") from e

        max_in = self.query_one("#mmf-max-in", Input).value.strip()
        if max_in:
            if not max_in.isdigit() or int(max_in) <= 0:
                raise ValueError("Max Input Tokens 必须是正整数")
            meta["max_input_tokens"] = int(max_in)
        max_out = self.query_one("#mmf-max-out", Input).value.strip()
        if max_out:
            if not max_out.isdigit() or int(max_out) <= 0:
                raise ValueError("Max Output Tokens 必须是正整数")
            meta["max_output_tokens"] = int(max_out)

        mode = self.query_one("#mmf-mode", Select).value
        meta["mode"] = str(mode) if mode and mode is not Select.NULL else "chat"

        selected = set(self.query_one("#mmf-features", SelectionList).selected)
        for k in self._feature_keys():
            if k in selected:
                meta[k] = True

        for qid, field in (("#mmf-rpm", "rpm"), ("#mmf-tpm", "tpm")):
            val = self.query_one(qid, Input).value.strip()
            if val:
                if not val.isdigit() or int(val) <= 0:
                    raise ValueError(f"{field.upper()} 必须是正整数")
                meta[field] = int(val)

        val = self.query_one("#mmf-reasoning", Input).value.strip()
        if val:
            try:
                meta["output_cost_per_reasoning_token"] = round(float(val) / 1_000_000, 12)
            except ValueError as e:
                raise ValueError(f"Reasoning 价格格式错误: {val}") from e

        val = self.query_one("#mmf-deprecation", Input).value.strip()
        if val:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                raise ValueError("Deprecation Date 格式应为 YYYY-MM-DD")
            meta["deprecation_date"] = val

        val = self.query_one("#mmf-source", Input).value.strip()
        if val:
            meta["source"] = val

        return meta

    @on(Button.Pressed, "#mmf-save")
    def _on_save(self) -> None:
        key = self.query_one("#mmf-key", Input).value.strip()
        if not key:
            self.app.notify_err("Model Key 不能为空")
            return
        try:
            meta = self._collect()
        except ValueError as e:
            self.app.notify_err(str(e))
            return
        self.dismiss(("save", key, meta))

    @on(Button.Pressed, "#mmf-delete")
    def _on_delete(self) -> None:
        self.app.push_screen(
            ConfirmModal(f"确认从 default.py 移除 [{self.key}]？", title="移除模型",
                         default_yes=False, yes="移除"),
            lambda ok: self.dismiss(("delete",)) if ok else None,
        )

    @on(Button.Pressed, "#mmf-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 阶梯定价（可复用子弹窗）

class TierModal(ModalScreen[None]):
    """管理某一阶梯（input tokens > t*1000）的四个价格，原地修改 meta。"""

    DEFAULT_CSS = """
    TierModal { align: center middle; }
    TierModal > .modal-box { width: 72; }
    TierModal .tier-main { height: auto; padding: 1 2; }
    TierModal OptionList { height: auto; border: none; }
    """

    def __init__(self, meta: dict, t: int):
        super().__init__()
        self.meta = meta
        self.t = t

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(
                Text(f"阶梯定价: 输入 tokens > {self.t}k 时整单适用", style="bold"),
                classes="modal-title",
            )
            with Vertical(classes="tier-main"):
                yield OptionList(id="tier-menu", markup=False)
            with Horizontal(classes="btn-row"):
                yield Button("完成", id="tier-done", variant="primary")
                yield Button("删除此阶梯", id="tier-del", variant="error")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.add_options([
            Option(f"{label:<12} ($/1M): ${eng.fmt_cost_m(self.meta.get(f'{base}_above_{self.t}k_tokens'))}")
            for base, label in eng.TIER_FIELDS
        ])
        self._bases = [base for base, _ in eng.TIER_FIELDS]
        ol.focus()

    @on(OptionList.OptionSelected, "#tier-menu")
    def _on_field(self, event: OptionList.OptionSelected) -> None:
        base = self._bases[event.option_index]
        label = dict(eng.TIER_FIELDS)[base]
        key = f"{base}_above_{self.t}k_tokens"
        self.app.push_screen(
            InputModal(f"输入 {label} 价格（$/1M tokens，留空清除）:", title=f"{label} · {self.t}k",
                       value=eng.cost_m_str(self.meta.get(key)),
                       validator=eng.valid_nonneg_float, error="请输入非负数字或留空"),
            lambda v, kk=key: self._set_cost(kk, v),
        )

    def _set_cost(self, field: str, v: str | None) -> None:
        if v is None:
            return
        ol = self.query_one(OptionList)
        if v.strip():
            self.meta[field] = float(v) / 1_000_000
        else:
            self.meta.pop(field, None)
        base = field.split("_above_")[0]
        label = dict(eng.TIER_FIELDS)[base]
        idx = self._bases.index(base)
        ol.replace_option_prompt_at_index(
            idx, f"{label:<12} ($/1M): ${eng.fmt_cost_m(self.meta.get(field))}"
        )

    @on(Button.Pressed, "#tier-del")
    def _del(self) -> None:
        self.app.push_screen(
            ConfirmModal(f"确认删除该模型 all above_{self.t}k_tokens 价格字段？",
                         title="删除阶梯", default_yes=False),
            lambda ok: self._do_del() if ok else None,
        )

    def _do_del(self) -> None:
        for base, _ in eng.TIER_FIELDS:
            self.meta.pop(f"{base}_above_{self.t}k_tokens", None)
        self.app.notify_ok(f"已删除 {self.t}k 阶梯")
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tier-done":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 自定义字段（可复用子弹窗）

class CustomFieldsModal(ModalScreen[None]):
    """管理任意自定义字段（排除已知字段、supports_ 与阶梯键），原地修改 meta。"""

    DEFAULT_CSS = """
    CustomFieldsModal { align: center middle; }
    CustomFieldsModal > .modal-box { width: 84; height: 80%; }
    CustomFieldsModal .cf-main { height: 1fr; padding: 1 2; }
    CustomFieldsModal OptionList { height: 1fr; border: round $secondary; }
    """

    def __init__(self, meta: dict):
        super().__init__()
        self.meta = meta

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text("管理自定义字段", style="bold"), classes="modal-title")
            with Vertical(classes="cf-main"):
                yield OptionList(id="cf-menu", markup=False)
            with Horizontal(classes="btn-row"):
                yield Button("完成", id="cf-done", variant="primary")
                yield Button("＋ 添加新字段", id="cf-add")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one(OptionList).focus()

    def _tier_keys(self) -> set:
        return {
            f"{base}_above_{t}k_tokens"
            for t in eng.collect_tiers(self.meta)
            for base, _ in eng.TIER_FIELDS
        }

    def _fields(self) -> dict:
        tier_keys = self._tier_keys()
        return {
            k: v for k, v in self.meta.items()
            if k not in eng.KNOWN_META_FIELDS
            and not k.startswith("supports_")
            and k not in tier_keys
        }

    def _rebuild(self) -> None:
        rows = []
        for k, v in sorted(self._fields().items()):
            rows.append((k, f"{k}: {shorten(str(v), 40)}"))
        rows.append(("__add__", "＋ 添加新字段"))
        ol = self.query_one(OptionList)
        ol.clear_options()
        ol.add_options([Option(label) for _, label in rows])
        self._ids = [k for k, _ in rows]

    @on(OptionList.OptionSelected, "#cf-menu")
    def _on_row(self, event: OptionList.OptionSelected) -> None:
        k = self._ids[event.option_index]
        if k == "__add__":
            self._add()
            return
        self.app.push_screen(
            PickModal(f"字段 '{k}' 当前值: {self.meta.get(k)}",
                      [PickItem("edit", "编辑"), PickItem("del", "删除")]),
            lambda item: self._field_action(k, item),
        )

    def _field_action(self, k: str, item: PickItem | None) -> None:
        if item is None:
            return
        if item.value == "del":
            self.app.push_screen(
                ConfirmModal(f"确认删除字段 '{k}'？", title="删除字段", default_yes=False),
                lambda ok: self._delete(k) if ok else None,
            )
        else:
            cur = self.meta.get(k)
            default = str(cur) if not isinstance(cur, (dict, list)) else json.dumps(cur, ensure_ascii=False)
            self.app.push_screen(
                InputModal(f"输入 {k} 的新值（留空删除）:", title=f"编辑 {k}", value=default),
                lambda v: self._edit(k, v),
            )

    def _delete(self, k: str) -> None:
        self.meta.pop(k, None)
        self.app.notify_ok(f"已删除字段 '{k}'")
        self._rebuild()

    def _edit(self, k: str, v: str | None) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[k] = _coerce(v.strip())
            self.app.notify_ok(f"已更新字段 '{k}'")
        else:
            self.meta.pop(k, None)
            self.app.notify_ok(f"已删除字段 '{k}'")
        self._rebuild()

    def _add(self) -> None:
        self.app.push_screen(
            InputModal("输入字段名称:", title="新字段",
                       validator=lambda v: bool(v and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", v.strip())),
                       error="只能包含字母、数字和下划线，且以字母或下划线开头"),
            lambda v: self._add_type(v),
        )

    def _add_type(self, name: str | None) -> None:
        if not name:
            return
        field_name = name.strip()
        if field_name in self.meta:
            self.app.notify_warn(f"字段 '{field_name}' 已存在，请从列表选择编辑")
            return
        items = [PickItem("s", "字符串"), PickItem("n", "数字"), PickItem("b", "布尔值"), PickItem("j", "JSON 对象")]
        self.app.push_screen(
            PickModal(f"选择字段 '{field_name}' 的类型", items),
            lambda item: self._add_value(field_name, item),
        )

    def _add_value(self, field_name: str, item: PickItem | None) -> None:
        if item is None:
            return
        t = item.value
        if t == "b":
            self.app.push_screen(
                PickModal(f"选择 {field_name} 的值", [PickItem(True, "True"), PickItem(False, "False")]),
                lambda v: self._finish_add(field_name, bool(v.value) if v else None),
            )
        else:
            hints = {"s": "输入值:", "n": "输入数字值:", "j": "输入 JSON 值:"}

            def validate(val: str) -> bool:
                val = (val or "").strip()
                if not val:
                    return False
                if t == "n":
                    try:
                        float(val)
                        return True
                    except ValueError:
                        return False
                if t == "j":
                    try:
                        json.loads(val)
                        return True
                    except json.JSONDecodeError:
                        return False
                return True

            self.app.push_screen(
                InputModal(hints[t], title=field_name, validator=validate,
                           error="数字需为合法数值 / JSON 需可解析"),
                lambda val: self._finish_add(field_name, _parse_typed(val, t) if val else None),
            )

    def _finish_add(self, field_name: str, value) -> None:
        if value is None:
            return
        self.meta[field_name] = value
        self.app.notify_ok(f"已添加字段 '{field_name}' = {value}")
        self._rebuild()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cf-done":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


def _coerce(v: str):
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    try:
        return float(v)
    except ValueError:
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v


def _parse_typed(v: str, t: str):
    v = v.strip()
    if t == "n":
        return float(v)
    if t == "j":
        return json.loads(v)
    return v
