"""模型参数管理界面：构建导出 / 脚本管理 / default.py 可视化编辑器 / 预览对比。"""
import asyncio
import copy
import json
import os
import re
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    LoadingIndicator,
    OptionList,
    SelectionList,
    Static,
)

try:
    from textual.widgets import Option
except ImportError:  # pragma: no cover
    from textual.widgets._option_list import Option

from .. import metadata as eng
from ..client import LiteLLMError
from ..widgets import (
    STYLE_DIM,
    STYLE_ERR,
    STYLE_INFO,
    STYLE_OK,
    STYLE_WARN,
    ConfirmModal,
    InputModal,
    MultiPickModal,
    OutputModal,
    PickItem,
    PickModal,
    load_rows,
    make_table,
)


# ---------------------------------------------------------------- 主页

class MetaHomeScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    ITEMS = [
        ("build", "构建模型参数"),
        ("edit_default", "编辑默认配置 (default.py)"),
        ("scripts", "脚本管理"),
        ("inspect", "预览模型参数（对比 Proxy）"),
    ]

    def compose(self):
        yield Static(Text("模型参数管理", style="bold"), classes="page-title")
        yield OptionList(id="mh-list", markup=False)
        yield Static("↑↓ 移动 · 回车 确认 · Esc 返回模型管理", classes="page-hint")

    def on_mount(self) -> None:
        ol = self.query_one("#mh-list", OptionList)
        ol.add_options([Option(label) for _, label in self.ITEMS])
        ol.highlighted = 0
        ol.focus()

    @on(OptionList.OptionSelected, "#mh-list")
    def _on_select(self, event: OptionList.OptionSelected) -> None:
        key = self.ITEMS[event.option_index][0]
        if key == "build":
            self.app.push_screen(BuildScreen())
        elif key == "edit_default":
            self.app.push_screen(DefaultEditorScreen())
        elif key == "scripts":
            self.app.push_screen(ScriptsScreen())
        elif key == "inspect":
            self.app.push_screen(InspectScreen())

    def action_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------- 构建导出

class BuildScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self):
        super().__init__()
        self._data: dict | None = None

    def compose(self):
        yield Static(Text("构建模型参数", style="bold"), classes="page-title")
        with VerticalScroll(id="build-log"):
            with Horizontal(id="build-loading"):
                yield LoadingIndicator()
                yield Static("正在执行启用脚本并按优先级合并…")
        with Horizontal(classes="toolbar"):
            yield Button("导出 JSON", id="export", disabled=True, variant="success")
            yield Button("返回", id="back")
        yield Static("导出后请确保 LiteLLM 已配置加载该文件并重启/重载", classes="page-hint")

    def on_mount(self) -> None:
        self._run()

    @work(exclusive=True)
    async def _run(self) -> None:
        client = self.app.get_client()
        mgr = eng.MetadataManager(client, self.app.config)
        try:
            data, logs = await asyncio.to_thread(mgr.build)
            diffs = await asyncio.to_thread(mgr.get_diff, data)
        except Exception as e:
            self.app.notify_err(f"构建失败: {e}")
            self.query_one("#build-loading").remove()
            self.query_one("#build-log", VerticalScroll).mount(Static(Text(f"构建失败: {e}", style=STYLE_ERR)))
            return
        self._data = data
        body = Text()
        body.append("==== 构建日志 ====\n", style="bold")
        for line in logs:
            style = STYLE_WARN if "警告" in line or "失败" in line else ""
            body.append(line + "\n", style=style)
        body.append("----\n", style="dim")
        for line in diffs:
            body.append(line + "\n", style=STYLE_INFO if "变动" in line else "")
        self.query_one("#build-loading").remove()
        self.query_one("#build-log", VerticalScroll).mount(Static(body))
        self.query_one("#export", Button).disabled = False
        self.app.status(f"构建完成: {len(data)} 条模型参数")

    @on(Button.Pressed, "#export")
    def _export(self) -> None:
        if self._data is None:
            return
        client = self.app.get_client()
        mgr = eng.MetadataManager(client, self.app.config)
        out_path = mgr.get_output_path()
        self.app.push_screen(
            ConfirmModal(f"确认导出并覆盖到 {out_path}？", title="导出模型参数"),
            lambda ok: self._do_export(ok, out_path),
        )

    @work(exclusive=True)
    async def _do_export(self, ok: bool, out_path: Path) -> None:
        if not ok:
            self.app.status("已取消导出，未写入文件")
            return
        client = self.app.get_client()
        mgr = eng.MetadataManager(client, self.app.config)
        try:
            path = await asyncio.to_thread(mgr.export, self._data)
            self.app.notify_ok(f"成功导出至: {path}")
        except Exception as e:
            self.app.notify_err(f"导出失败: {e}")

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------- 脚本管理

class ScriptsScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self):
        super().__init__()
        self._files: list = []

    def compose(self):
        yield Static(Text("脚本管理", style="bold"), classes="page-title", id="sc-title")
        with Horizontal(classes="toolbar"):
            yield Button("新建脚本", id="add", variant="primary")
            yield Button("重置所有内置示例", id="reset")
            yield Button("刷新", id="refresh")
            yield Button("返回", id="back")
        table = make_table("名称", "脚本文件名", "优先级", "状态")
        table.id = "sc-table"
        yield table
        yield Static("↑↓ 移动 · 回车 管理所选脚本 · Esc 返回", classes="page-hint")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one(DataTable).focus()

    def _meta_dir(self) -> Path:
        return eng.MetadataManager(None, self.app.config).meta_dir

    def _rebuild(self) -> None:
        meta_dir = self._meta_dir()
        files = sorted(
            [f for f in meta_dir.glob("*.py") if not f.name.startswith(".")],
            key=lambda f: f.name,
        )
        rows = []
        for f in files:
            m = eng.parse_script_meta(f)
            status = Text("启用", style=STYLE_OK) if m.enabled else Text("禁用", style="dim")
            rows.append([Text(m.name), Text(f.name), Text(f"P={m.priority}"), status])
        self._files = files
        load_rows(self.query_one("#sc-table", DataTable), rows)
        self.query_one("#sc-title", Static).update(Text(f"脚本管理（共 {len(files)} 个脚本）", style="bold"))

    @on(DataTable.RowSelected, "#sc-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._files):
            self._item_menu(self._files[event.cursor_row])

    def _item_menu(self, path: Path) -> None:
        m = eng.parse_script_meta(path)
        is_example = (eng.EXAMPLES_DIR / path.name).is_file()
        items = [
            PickItem("toggle", "禁用该脚本" if m.enabled else "启用该脚本"),
            PickItem("priority", f"调整优先级 (当前 PRIORITY={m.priority})"),
        ]
        if path.name == "default.py":
            items.append(PickItem("edit_visual", "进入可视化编辑器 (编辑 default.py)"))
        items.append(PickItem("preview", "运行测试 / 预览脚本输出"))
        if is_example:
            items.append(PickItem("reset", f"重置为内置示例 ({path.name})"))
        if path.name != "default.py":
            items.append(PickItem("delete", f"删除脚本文件 ({path.name})"))
        items.append(PickItem("back", "[返回]"))
        self.app.push_screen(
            PickModal(f"脚本管理: {m.name} ({path.name})", items, message=f"说明: {m.description}"),
            lambda item: self._item_action(path, item),
        )

    def _item_action(self, path: Path, item: PickItem | None) -> None:
        if item is None or item.value == "back":
            return
        act = item.value
        if act == "toggle":
            eng.update_script_enabled(path, not eng.parse_script_meta(path).enabled)
            self._rebuild()
        elif act == "priority":
            self._edit_priority(path)
        elif act == "edit_visual":
            self.app.push_screen(DefaultEditorScreen())
        elif act == "preview":
            self._preview(path)
        elif act == "reset":
            self.app.push_screen(
                ConfirmModal(f"确认将 {path.name} 重置为内置版本？", title="重置脚本", default_yes=False),
                lambda ok: self._do_reset(path) if ok else None,
            )
        elif act == "delete":
            self.app.push_screen(
                ConfirmModal(f"确认删除脚本文件 {path.name}？", title="删除脚本", default_yes=False, yes="确认删除"),
                lambda ok: self._do_delete(path) if ok else None,
            )

    def _edit_priority(self, path: Path) -> None:
        cur = eng.parse_script_meta(path).priority
        self.app.push_screen(
            InputModal(
                f"请输入脚本 [{path.name}] 的新优先级（不小于 0）:",
                title="调整优先级", value=str(cur),
                validator=lambda v: bool(v and v.strip().isdigit() and int(v) >= 0),
                error="请输入大于或等于 0 的整数",
            ),
            lambda val: self._apply_priority(path, val),
        )

    def _apply_priority(self, path: Path, val: str | None) -> None:
        if val:
            eng.update_script_priority(path, int(val.strip()))
            self.app.notify_ok(f"优先级已修改为: {int(val.strip())}")
            self._rebuild()

    @work(exclusive=True)
    async def _preview(self, path: Path) -> None:
        self.app.status(f"正在运行脚本 {path.name}…")
        data, error, stderr = await asyncio.to_thread(eng.run_script, path, 60)
        body = Text()
        if error:
            body.append(f"脚本执行失败: {error}\n", style=STYLE_ERR)
            if stderr:
                body.append(f"详细信息:\n{stderr}\n", style=STYLE_DIM)
        else:
            if stderr:
                body.append(f"日志输出:\n{stderr}\n", style=STYLE_DIM)
            body.append(f"共 {len(data)} 个模型:\n", style="bold")
            body.append(json.dumps(data, ensure_ascii=False, indent=2))
        self.app.push_screen(OutputModal(f"脚本输出预览: {path.name}", body))

    def _do_reset(self, path: Path) -> None:
        example = eng.EXAMPLES_DIR / path.name
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        eng.update_script_enabled(path, False)
        if os.name != "nt":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        self.app.notify_ok(f"脚本已成功重置: {path.name}")
        self._rebuild()

    def _do_delete(self, path: Path) -> None:
        path.unlink()
        self.app.notify_ok(f"文件已删除: {path.name}")
        self._rebuild()

    @on(Button.Pressed, "#add")
    def _add_script(self) -> None:
        self.app.push_screen(
            InputModal(
                "请输入新脚本名称:", title="新建脚本",
                validator=lambda v: bool(v and v.strip() and re.match(r"^[A-Za-z0-9._-]+$", v.strip())),
                error="名称仅含字母、数字、. _ -",
            ),
            lambda val: self._add_templates(val),
        )

    def _add_templates(self, name: str | None) -> None:
        if not name:
            return
        stem = name.strip()
        if stem.endswith(".py"):
            stem = stem[:-3]
        path = self._meta_dir() / f"{stem}.py"
        if path.exists():
            self.app.notify_warn(f"脚本已存在: {path.name}")
            return
        items = []
        if eng.EXAMPLES_DIR.exists():
            for ex in sorted(eng.EXAMPLES_DIR.glob("*.py")):
                if ex.name.startswith("."):
                    continue
                m = eng.parse_script_meta(ex)
                items.append(PickItem(ex.name, f"从示例: {m.name}", hint=ex.name))
        items.append(PickItem("blank", "空白 Python 脚本模版"))
        self.app.push_screen(
            PickModal(f"选择脚本 [{stem}.py] 的创建模版", items),
            lambda item: self._add_confirm(path, stem, item),
        )

    def _add_confirm(self, path: Path, stem: str, item: PickItem | None) -> None:
        if item is None:
            return
        self.app.push_screen(
            ConfirmModal(f"确认创建脚本 {path.name}？\n创建后默认为禁用状态。", title="新建脚本"),
            lambda ok: self._do_add(path, stem, item.value, ok),
        )

    def _do_add(self, path: Path, stem: str, tpl: str, ok: bool) -> None:
        if not ok:
            return
        if tpl == "blank":
            content = eng.blank_script_content(stem)
        else:
            content = (eng.EXAMPLES_DIR / tpl).read_text(encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        eng.update_script_enabled(path, False)
        if os.name != "nt":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        self.app.notify_ok(f"脚本已创建（默认禁用）: {path.name}")
        self._rebuild()

    @on(Button.Pressed, "#reset")
    def _reset_all(self) -> None:
        self.app.push_screen(
            ConfirmModal(
                "对内置脚本的本地修改将被重置，并恢复为默认状态（禁用），是否继续？",
                title="重置所有内置示例", default_yes=False,
            ),
            lambda ok: self._do_reset_all() if ok else None,
        )

    def _do_reset_all(self) -> None:
        n = eng.reset_examples(self._meta_dir())
        self.app.notify_ok(f"已重置 {n} 个内置脚本")
        self._rebuild()

    @on(Button.Pressed, "#refresh")
    def _on_refresh(self) -> None:
        self._rebuild()

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------- default.py 编辑器

class DefaultEditorScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self):
        super().__init__()
        self._orig: dict = {}
        self._working: dict = {}
        self._keys: list = []
        self._filter = ""

    def compose(self):
        yield Static(Text("编辑默认配置 (default.py)", style="bold"), classes="page-title", id="de-title")
        with Horizontal(classes="search-row"):
            yield Static("搜索  ", classes="page-hint")
            yield Input(placeholder="按模型键筛选", id="de-search")
        with Horizontal(classes="toolbar"):
            yield Button("＋ 选择并添加模型", id="add", variant="primary")
            yield Button("保存修改", id="save")
            yield Button("放弃修改", id="cancel")
            yield Button("返回", id="back")
        table = make_table("Model Key", "配置摘要")
        table.id = "de-table"
        yield table
        yield Static("↑↓ 移动 · 回车 编辑所选模型 · Esc 返回", classes="page-hint")

    def on_mount(self) -> None:
        self._load()
        self.query_one(DataTable).focus()

    def _default_path(self) -> Path:
        return eng.MetadataManager(None, self.app.config).meta_dir / "default.py"

    @work(exclusive=True)
    async def _load(self) -> None:
        path = self._default_path()
        if not path.exists():
            await asyncio.to_thread(eng.sync_example_scripts)
        data, error = await asyncio.to_thread(eng.read_default_py_data, path)
        if error:
            self.app.notify_err("无法加载 default.py")
            self.app.push_screen(OutputModal(
                "无法加载 default.py",
                Text(f"{error}\n\n请修复文件后重试。", style=STYLE_ERR),
            ))
            self.app.pop_screen()
            return
        self._orig = data
        self._working = copy.deepcopy(data)
        self._rebuild()

    def _rebuild(self) -> None:
        keys = sorted(self._working.keys())
        if self._filter:
            keys = [k for k in keys if self._filter.lower() in k.lower()]
        rows = []
        for k in keys:
            rows.append([Text(k), Text(eng.config_summary(self._working[k]), style="dim")])
        self._keys = keys
        load_rows(self.query_one("#de-table", DataTable), rows)
        self.query_one("#de-title", Static).update(
            Text(f"正在编辑默认配置（{len(self._working)} 个模型）", style="bold")
        )

    @on(Input.Changed, "#de-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(DataTable.RowSelected, "#de-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._keys):
            key = self._keys[event.cursor_row]
            self.app.push_screen(
                ModelMetaEditScreen(key, copy.deepcopy(self._working[key])),
                lambda res: self._entry_done(key, res),
            )

    def _entry_done(self, key: str, res) -> None:
        if res is None:
            return
        kind = res[0]
        if kind == "delete":
            self._working.pop(key, None)
        elif kind == "save":
            self._working[key] = res[1]
        self._rebuild()

    # ---------------------------------------------------------- 添加模型

    @on(Button.Pressed, "#add")
    def _add(self) -> None:
        items = [
            PickItem("builtin", "从 LiteLLM 内置 Provider 数据拉取模型"),
            PickItem("upstream", "从 Upstream 拉取模型"),
            PickItem("manual", "手动输入模型名称（英文逗号分隔）"),
            PickItem("back", "[返回]"),
        ]
        self.app.push_screen(
            PickModal("添加模型：选择来源", items, message="选择要添加到 default.py 的模型:"),
            lambda item: self._add_source(item),
        )

    @work(exclusive=True)
    async def _add_source(self, item: PickItem | None) -> None:
        if item is None or item.value == "back":
            return
        new_keys: list = []
        if item.value == "builtin":
            cost_map = await self.app.get_cost_map()
            if not cost_map:
                self.app.notify_err("未能获取到 LiteLLM 内置模型列表")
                return
            picked = await self.app.push_screen_wait(
                MultiPickModal(f"选择要添加的模型（共 {len(cost_map)} 个已知模型）",
                               [PickItem(k, k) for k in sorted(cost_map.keys())])
            )
            new_keys = picked or []
        elif item.value == "upstream":
            upstreams = (self.app.config or {}).get("upstreams") or []
            if not upstreams:
                self.app.notify_warn("配置中没有 Upstream")
                return
            u = await self.app.push_screen_wait(
                PickModal("请选择 Upstream",
                          [PickItem(str(i), f"{u0['name']} ({u0['type']})") for i, u0 in enumerate(upstreams)])
            )
            if u is None:
                return
            from ..upstreams import UpstreamError, fetch_upstream_models
            warnings: list = []
            try:
                models = await asyncio.to_thread(
                    lambda: fetch_upstream_models(upstreams[int(u.value)], log=warnings.append)
                )
            except UpstreamError as e:
                self.app.notify_err(f"拉取失败: {e}")
                return
            for w in warnings:
                self.app.notify_warn(w)
            if not models:
                self.app.notify_warn("列表为空")
                return
            picked = await self.app.push_screen_wait(
                MultiPickModal("选择要添加的模型", [PickItem(m, m) for m in sorted(models)])
            )
            new_keys = picked or []
        elif item.value == "manual":
            val = await self.app.push_screen_wait(
                InputModal("请输入模型名称，使用英文逗号分隔:", title="手动添加",
                           validator=lambda v: bool(v and v.strip()), error="不能为空")
            )
            if val:
                new_keys = [k.strip() for k in val.split(",") if k.strip()]
        added = 0
        for k in new_keys:
            if k not in self._working:
                self._working[k] = {"mode": "chat"}
                added += 1
        if added:
            self.app.notify_ok(f"已添加 {added} 个模型，请编辑后保存")
        self._rebuild()

    # ---------------------------------------------------------- 保存/放弃

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        old_k, new_k = set(self._orig), set(self._working)
        added = len(new_k - old_k)
        deleted = len(old_k - new_k)
        changed = sum(1 for k in old_k & new_k if self._orig[k] != self._working[k])
        if added == deleted == changed == 0:
            self.app.status("没有需要保存的变更")
            self.app.pop_screen()
            return
        self.app.push_screen(
            ConfirmModal(
                f"变动概览: 添加 {added} 个 / 删除 {deleted} 个 / 修改 {changed} 个\n保存修改？",
                title="保存 default.py",
            ),
            lambda ok: self._do_save(ok),
        )

    @work(exclusive=True)
    async def _do_save(self, ok: bool) -> None:
        if not ok:
            return
        path = self._default_path()
        await asyncio.to_thread(eng.save_default_py, path, self._working)
        self.app.notify_ok("default.py 已保存")
        self.app.pop_screen()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.app.push_screen(
            ConfirmModal("确认放弃本次未保存的修改？", title="放弃修改", default_yes=False, yes="放弃"),
            lambda ok: self.app.pop_screen() if ok else None,
        )

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self._cancel()

    def action_back(self) -> None:
        self._cancel()


# ---------------------------------------------------------------- 单模型元数据编辑

class ModelMetaEditScreen(ModalScreen):
    """返回 ("save", meta) / ("delete",) / None(取消)。"""

    DEFAULT_CSS = """
    ModelMetaEditScreen { align: center middle; }
    ModelMetaEditScreen > .modal-box { width: 88; height: 90%; }
    ModelMetaEditScreen OptionList { height: 1fr; border: none; }
    """

    def __init__(self, key: str, meta: dict):
        super().__init__()
        self.key = key
        self.meta = meta

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text(f"配置模型 [{self.key}] 参数", style="bold"), classes="modal-title")
            yield OptionList(id="mme-menu", markup=False)
            with Horizontal(classes="btn-row"):
                yield Button("保存并返回", id="mme-save", variant="primary")
                yield Button("取消修改", id="mme-cancel")
                yield Button("从分片中移除", id="mme-delete", variant="error")

    def on_mount(self) -> None:
        self._rebuild_menu()

    def _feat_str(self) -> str:
        feats = [k.replace("supports_", "") for k in eng.BUILTIN_FEATURES if self.meta.get(k) is True]
        feats += sorted(k.replace("supports_", "") for k in self.meta
                        if k.startswith("supports_") and k not in eng.BUILTIN_FEATURES and self.meta[k] is True)
        return ", ".join(feats) or "无"

    def _rebuild_menu(self) -> None:
        m = self.meta
        tiers = eng.collect_tiers(m)
        tier_str = ",".join(f"{t}k" for t in tiers) if tiers else "无"
        rows = [
            ("provider", f"LiteLLM Provider      : {m.get('litellm_provider') or '-'}"),
            ("in_cost", f"Input Cost ($/1M)      : ${eng.fmt_cost_m(m.get('input_cost_per_token'))}"),
            ("out_cost", f"Output Cost ($/1M)     : ${eng.fmt_cost_m(m.get('output_cost_per_token'))}"),
            ("cache_read", f"Cache Read Cost ($/1M) : ${eng.fmt_cost_m(m.get('cache_read_input_token_cost'))}"),
            ("cache_write", f"Cache Write Cost     : ${eng.fmt_cost_m(m.get('cache_creation_input_token_cost'))}"),
            ("max_in", f"Max Input Tokens     : {m.get('max_input_tokens') or m.get('max_tokens') or '-'}"),
            ("max_out", f"Max Output Tokens    : {m.get('max_output_tokens') or '-'}"),
            ("mode", f"Mode               : {m.get('mode') or 'chat'}"),
            ("features", f"特性支持开关         : {self._feat_str()}"),
            ("advanced", f"更多高级字段 (阶梯:{tier_str} · rpm:{m.get('rpm', '-')} · tpm:{m.get('tpm', '-')}) >>"),
        ]
        ol = self.query_one("#mme-menu", OptionList)
        ol.clear_options()
        ol.add_options([Option(label) for _, label in rows])
        self._ids = [k for k, _ in rows]
        ol.highlighted = 0

    @on(OptionList.OptionSelected, "#mme-menu")
    def _on_field(self, event: OptionList.OptionSelected) -> None:
        f = self._ids[event.option_index]
        m = self.meta
        if f == "provider":
            self.app.push_screen(
                InputModal("输入 LiteLLM Provider（留空清除）:", title="Provider",
                           value=m.get("litellm_provider") or ""),
                lambda v: self._set_str("litellm_provider", v),
            )
        elif f in ("in_cost", "out_cost", "cache_read", "cache_write"):
            field_name = {
                "in_cost": "input_cost_per_token",
                "out_cost": "output_cost_per_token",
                "cache_read": "cache_read_input_token_cost",
                "cache_write": "cache_creation_input_token_cost",
            }[f]
            label = {
                "in_cost": "Input", "out_cost": "Output",
                "cache_read": "Cache Read", "cache_write": "Cache Write",
            }[f]
            self.app.push_screen(
                InputModal(f"输入 {label} 价格（$/1M tokens，留空清除）:", title=f"{label} Cost",
                           value=eng.cost_m_str(m.get(field_name)),
                           validator=eng.valid_nonneg_float, error="请输入非负数字或留空"),
                lambda v, fn=field_name: self._set_cost(fn, v),
            )
        elif f in ("max_in", "max_out"):
            field_name = "max_input_tokens" if f == "max_in" else "max_output_tokens"
            if f == "max_in":
                cur = m.get("max_input_tokens") or m.get("max_tokens")
            else:
                cur = m.get("max_output_tokens")
            self.app.push_screen(
                InputModal(f"输入 Max {'Input' if f == 'max_in' else 'Output'} Tokens（留空清除）:",
                           title=f"Max {'Input' if f == 'max_in' else 'Output'}",
                           value=str(cur or ""),
                           validator=eng.valid_positive_int, error="请输入正整数或留空"),
                lambda v, fn=field_name, small=f: self._set_int(fn, v, clear_max_tokens=small == "max_in"),
            )
        elif f == "mode":
            items = [PickItem(v, v, hint="当前" if v == (m.get("mode") or "chat") else "") for v in eng.MODE_CHOICES]
            self.app.push_screen(PickModal("选择 Mode", items),
                             lambda item: self._set_mode(item))
        elif f == "features":
            self.app.push_screen(FeaturesModal(m), lambda _: self._rebuild_menu())
        elif f == "advanced":
            self.app.push_screen(AdvancedModal(m), lambda _: self._rebuild_menu())

    def _set_str(self, field: str, v: str | None) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[field] = v.strip()
        else:
            self.meta.pop(field, None)
        self._rebuild_menu()

    def _set_cost(self, field: str, v: str | None) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[field] = float(v) / 1_000_000
        else:
            self.meta.pop(field, None)
        self._rebuild_menu()

    def _set_int(self, field: str, v: str | None, clear_max_tokens: bool = False) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[field] = int(v)
            if clear_max_tokens:
                self.meta.pop("max_tokens", None)
        else:
            self.meta.pop(field, None)
        self._rebuild_menu()

    def _set_mode(self, item: PickItem | None) -> None:
        if item is not None:
            self.meta["mode"] = item.value
            self._rebuild_menu()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mme-save":
            self.dismiss(("save", self.meta))
        elif event.button.id == "mme-delete":
            self.app.push_screen(
                ConfirmModal(f"确认从分片中移除模型 [{self.key}]？", title="移除模型", default_yes=False),
                lambda ok: self.dismiss(("delete",)) if ok else None,
            )
        else:
            self.dismiss(None)


# ---------------------------------------------------------------- 特性开关

class FeaturesModal(ModalScreen[None]):
    """特性支持开关（内置 + 自定义 supports_ 字段），原地修改 meta。"""

    DEFAULT_CSS = """
    FeaturesModal { align: center middle; }
    FeaturesModal > .modal-box { width: 76; height: 80%; }
    FeaturesModal SelectionList { height: 1fr; border: round $secondary; }
    """

    def __init__(self, meta: dict):
        super().__init__()
        self.meta = meta

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text("特性支持开关", style="bold"), classes="modal-title")
            yield Static("勾选 = True；取消勾选 = False。修改即时生效，Esc 返回。", classes="page-hint")
            yield SelectionList(id="feat-list")
            with Horizontal(classes="btn-row"):
                yield Button("＋ 添加自定义字段", id="feat-add")
                yield Button("完成", id="feat-done", variant="primary")

    def on_mount(self) -> None:
        self._rebuild()

    def _all_feature_keys(self) -> list:
        keys = list(eng.BUILTIN_FEATURES)
        keys += sorted(k for k in self.meta if k.startswith("supports_") and k not in eng.BUILTIN_FEATURES)
        return keys

    def _rebuild(self) -> None:
        sel = self.query_one("#feat-list", SelectionList)
        sel.clear_options()
        sel.add_options([
            (k, k, self.meta.get(k) is True) for k in self._all_feature_keys()
        ])

    @on(SelectionList.SelectedChanged)
    def _on_toggle(self, event: SelectionList.SelectedChanged) -> None:
        checked = set(event.selection_list.selected)
        for k in self._all_feature_keys():
            self.meta[k] = k in checked

    @on(Button.Pressed, "#feat-add")
    def _add_custom(self) -> None:
        self.app.push_screen(
            InputModal("输入自定义字段名称（supports_ 前缀会自动添加）:", title="自定义字段",
                       validator=lambda v: bool(v and v.strip()), error="不能为空"),
            lambda v: self._add_custom_value(v),
        )

    def _add_custom_value(self, name: str | None) -> None:
        if not name:
            return
        field_name = name.strip()
        if not field_name.startswith("supports_"):
            field_name = "supports_" + field_name
        if field_name in self.meta:
            self.app.notify_warn(f"字段 '{field_name}' 已存在")
            return
        items = [PickItem(True, f"{field_name} = True"), PickItem(False, f"{field_name} = False")]
        self.app.push_screen(
            PickModal(f"设置 {field_name} 的值", items),
            lambda item: self._custom_picked(field_name, item),
        )

    def _custom_picked(self, field_name: str, item: PickItem | None) -> None:
        if item is None:
            return
        self.meta[field_name] = bool(item.value)
        self._rebuild()
        self.app.notify_ok(f"已添加字段 '{field_name}' = {self.meta[field_name]}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "feat-done":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 高级字段

class AdvancedModal(ModalScreen[None]):
    """阶梯定价 / rpm / tpm / reasoning / 弃用日期 / 来源 / 自定义字段，原地修改 meta。"""

    DEFAULT_CSS = """
    AdvancedModal { align: center middle; }
    AdvancedModal > .modal-box { width: 82; height: 85%; }
    AdvancedModal OptionList { height: 1fr; border: round $secondary; }
    """

    def __init__(self, meta: dict):
        super().__init__()
        self.meta = meta

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text("高级字段", style="bold"), classes="modal-title")
            yield OptionList(id="adv-menu", markup=False)
            with Horizontal(classes="btn-row"):
                yield Button("完成", id="adv-done", variant="primary")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one(OptionList).focus()

    def _rows(self) -> list:
        m = self.meta
        rows = []
        for t in eng.collect_tiers(m):
            trig = "可触发" if f"input_cost_per_token_above_{t}k_tokens" in m else "缺触发键"
            rows.append((f"tier:{t}", f"阶梯定价 >{t}k tokens [{trig}] (管理)"))
        src = m.get("source")
        src_str = (src[:50] + "…") if isinstance(src, str) and len(src) > 50 else (src or "-")
        rows.extend([
            ("new_tier", "新增阶梯定价 (…_above_Nk_tokens)"),
            ("rpm", f"RPM - 每分钟请求数: {m.get('rpm') if m.get('rpm') is not None else '-'}"),
            ("tpm", f"TPM - 每分钟 tokens: {m.get('tpm') if m.get('tpm') is not None else '-'}"),
            ("reasoning", f"Reasoning Output Cost ($/1M): ${eng.fmt_cost_m(m.get('output_cost_per_reasoning_token'))}"),
            ("deprecation_date", f"Deprecation Date (YYYY-MM-DD): {m.get('deprecation_date') or '-'}"),
            ("source", f"来源: {src_str}"),
            ("custom_fields", f"管理自定义字段 (共 {self._custom_count()}) >>"),
        ])
        return rows

    def _custom_count(self) -> int:
        return len([k for k in self.meta if k not in eng.KNOWN_META_FIELDS and not k.startswith("supports_")])

    def _rebuild(self) -> None:
        rows = self._rows()
        ol = self.query_one(OptionList)
        ol.clear_options()
        ol.add_options([Option(label) for _, label in rows])
        self._ids = [k for k, _ in rows]

    @on(OptionList.OptionSelected, "#adv-menu")
    def _on_row(self, event: OptionList.OptionSelected) -> None:
        f = self._ids[event.option_index]
        m = self.meta
        if f.startswith("tier:"):
            self.app.push_screen(TierModal(m, int(f.split(":")[1])), lambda _: self._rebuild())
        elif f == "new_tier":
            self.app.push_screen(
                InputModal("输入阶梯触发阈值 N（单位 k tokens）:", title="新增阶梯",
                           validator=eng.valid_positive_int, error="请输入正整数",
                           hint="触发键为 input_cost_per_token_above_Nk_tokens（无此项该阶梯不会触发）"),
                lambda v: self._new_tier(v),
            )
        elif f in ("rpm", "tpm"):
            self.app.push_screen(
                InputModal(f"输入 {f.upper()}（留空清除）:", title=f.upper(),
                           value=str(m.get(f) or ""),
                           validator=eng.valid_positive_int, error="请输入正整数或留空"),
                lambda v, kk=f: self._set_pos_int(kk, v),
            )
        elif f == "reasoning":
            self.app.push_screen(
                InputModal("输入 Reasoning Output 价格（$/1M tokens，留空清除）:", title="Reasoning",
                           value=eng.cost_m_str(m.get("output_cost_per_reasoning_token")),
                           validator=eng.valid_nonneg_float, error="请输入非负数字或留空"),
                lambda v: self._set_cost("output_cost_per_reasoning_token", v),
            )
        elif f == "deprecation_date":
            self.app.push_screen(
                InputModal("输入日期 (YYYY-MM-DD，留空清除):", title="Deprecation Date",
                           value=m.get("deprecation_date") or "",
                           validator=lambda v: not v.strip() or bool(re.match(r"^\d{4}-\d{2}-\d{2}$", v.strip())),
                           error="格式应为 YYYY-MM-DD"),
                lambda v: self._set_str("deprecation_date", v),
            )
        elif f == "source":
            self.app.push_screen(
                InputModal("输入来源（留空清除）:", title="来源", value=m.get("source") or ""),
                lambda v: self._set_str("source", v),
            )
        elif f == "custom_fields":
            self.app.push_screen(CustomFieldsModal(m), lambda _: self._rebuild())

    def _new_tier(self, v: str | None) -> None:
        if not v:
            return
        t = int(v)
        if t in eng.collect_tiers(self.meta):
            self.app.notify_ok(f"{t}k 阶梯已存在，进入其管理")
        self.app.push_screen(TierModal(self.meta, t), lambda _: self._rebuild())

    def _set_pos_int(self, field: str, v: str | None) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[field] = int(v)
        else:
            self.meta.pop(field, None)
        self._rebuild()

    def _set_cost(self, field: str, v: str | None) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[field] = float(v) / 1_000_000
        else:
            self.meta.pop(field, None)
        self._rebuild()

    def _set_str(self, field: str, v: str | None) -> None:
        if v is None:
            return
        if v.strip():
            self.meta[field] = v.strip()
        else:
            self.meta.pop(field, None)
        self._rebuild()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "adv-done":
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class TierModal(ModalScreen[None]):
    """管理某一阶梯（input tokens > t*1000）的四个价格，原地修改 meta。"""

    DEFAULT_CSS = """
    TierModal { align: center middle; }
    TierModal > .modal-box { width: 72; height: auto; }
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
            yield OptionList(id="tier-menu", markup=False)
            with Horizontal(classes="btn-row"):
                yield Button("删除此阶梯", id="tier-del", variant="error")
                yield Button("完成", id="tier-done", variant="primary")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.add_options([Option(f"{label:<12} ($/1M): ${eng.fmt_cost_m(self.meta.get(f'{base}_above_{self.t}k_tokens'))}")
                        for base, label in eng.TIER_FIELDS])
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


class CustomFieldsModal(ModalScreen[None]):
    """管理任意自定义字段（排除已知字段与 supports_），原地修改 meta。"""

    DEFAULT_CSS = """
    CustomFieldsModal { align: center middle; }
    CustomFieldsModal > .modal-box { width: 84; height: 80%; }
    CustomFieldsModal OptionList { height: 1fr; border: round $secondary; }
    """

    def __init__(self, meta: dict):
        super().__init__()
        self.meta = meta

    def compose(self):
        with Vertical(classes="modal-box"):
            yield Static(Text("管理自定义字段", style="bold"), classes="modal-title")
            yield OptionList(id="cf-menu", markup=False)
            with Horizontal(classes="btn-row"):
                yield Button("＋ 添加新字段", id="cf-add", variant="primary")
                yield Button("完成", id="cf-done")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one(OptionList).focus()

    def _fields(self) -> dict:
        return {
            k: v for k, v in self.meta.items()
            if k not in eng.KNOWN_META_FIELDS and not k.startswith("supports_")
        }

    def _rebuild(self) -> None:
        rows = []
        for k, v in sorted(self._fields().items()):
            v_str = str(v)
            if len(v_str) > 40:
                v_str = v_str[:37] + "..."
            rows.append((k, f"{k}: {v_str}"))
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
            items = [PickItem(True, "True"), PickItem(False, "False")]
            self.app.push_screen(
                PickModal(f"选择 {field_name} 的值", items),
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


# ---------------------------------------------------------------- 预览对比

class InspectScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self):
        super().__init__()
        self._data: dict = {}
        self._current: dict = {}
        self._keys: list = []
        self._filter = ""

    def compose(self):
        yield Static(Text("参数预览（对比当前 Proxy）", style="bold"), classes="page-title", id="isp-title")
        with Horizontal(classes="search-row"):
            yield Static("搜索  ", classes="page-hint")
            yield Input(placeholder="按模型键筛选", id="isp-search")
        with Horizontal(classes="toolbar"):
            yield Button("返回", id="back")
        table = make_table("状态", "Model Key", "In $/1M", "Out $/1M")
        table.id = "isp-table"
        yield table
        yield Static("↑↓ 移动 · 回车 查看条目详情 · Esc 返回", classes="page-hint")

    def on_mount(self) -> None:
        self._load()

    @work(exclusive=True)
    async def _load(self) -> None:
        client = self.app.get_client()
        self.app.status("正在构建预览参数…")
        mgr = eng.MetadataManager(client, self.app.config)
        try:
            data, _ = await asyncio.to_thread(mgr.build)
            current = await asyncio.to_thread(mgr.client.fetch_model_cost_map)
        except LiteLLMError as e:
            self.app.notify_err(f"无法获取 Proxy 当前参数: {e}")
            data, current = {}, {}
        except Exception as e:
            self.app.notify_err(f"构建预览失败: {e}")
            return
        self._data = data
        self._current = current or {}
        self._rebuild()
        self.query_one(DataTable).focus()

    def _status_of(self, k: str) -> tuple[str, str]:
        n_entry = self._data[k]
        c_entry = self._current.get(k, {})
        if k not in self._current:
            return "新", STYLE_INFO
        for f in ("input_cost_per_token", "output_cost_per_token", "max_input_tokens"):
            if c_entry.get(f) != n_entry.get(f):
                return "变", STYLE_WARN
        return "同", "dim"

    def _rebuild(self) -> None:
        keys = sorted(self._data.keys())
        if self._filter:
            keys = [k for k in keys if self._filter.lower() in k.lower()]
        self._keys = keys
        rows = []
        for k in keys:
            status, style = self._status_of(k)
            n = self._data[k]
            in_cost = n.get("input_cost_per_token")
            out_cost = n.get("output_cost_per_token")
            rows.append([
                Text(f"[{status}]", style=style),
                Text(k),
                Text(f"{in_cost * 1_000_000:.3f}" if isinstance(in_cost, (int, float)) else "-"),
                Text(f"{out_cost * 1_000_000:.3f}" if isinstance(out_cost, (int, float)) else "-"),
            ])
        load_rows(self.query_one("#isp-table", DataTable), rows)
        self.query_one("#isp-title", Static).update(
            Text(f"参数预览（共 {len(self._data)} 条模型，对比当前 Proxy）", style="bold")
        )

    @on(Input.Changed, "#isp-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._rebuild()

    @on(DataTable.RowSelected, "#isp-table")
    def _on_row(self, event: DataTable.RowSelected) -> None:
        if not (0 <= event.cursor_row < len(self._keys)):
            return
        k = self._keys[event.cursor_row]
        body = Text()
        body.append("新生成条目:\n", style="bold")
        body.append(json.dumps(self._data[k], ensure_ascii=False, indent=2) + "\n")
        c_entry = self._current.get(k)
        if c_entry:
            body.append("\nProxy 当前实时条目:\n", style="bold")
            body.append(json.dumps(c_entry, ensure_ascii=False, indent=2) + "\n")
        else:
            body.append("\nProxy 当前无此条目（全新添加）\n", style=STYLE_WARN)
        self.app.push_screen(OutputModal(f"模型参数详情: {k}", body))

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()
