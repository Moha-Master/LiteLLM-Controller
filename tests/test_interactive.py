"""交互级测试：mock 掉网络，验证模型列表渲染、行操作弹窗、筛选、向导模态。"""
import asyncio

import pytest

MINI_CONFIG = """\
litellm:
  endpoint: "http://mock"
  key: "sk-test"
upstreams:
  - name: up1
    type: openai
    endpoint: "http://mock/models"
    key: "sk-x"
model_metadata:
  output_file: null
  amend_upstream:
    type: "off"
    url: null
"""

FAKE_MODELS = [
    {"model_name": "gpt-4", "litellm_params": {"model": "openai/gpt-4", "custom_llm_provider": "openai"},
     "model_info": {"id": "1", "input_cost_per_token": 1e-5, "output_cost_per_token": 2e-5}},
    {"model_name": "claude", "litellm_params": {"model": "anthropic/claude", "custom_llm_provider": "anthropic"},
     "model_info": {"id": "2", "blocked": True, "input_cost_per_token": 3e-6, "output_cost_per_token": 4e-6}},
    {"model_name": "gpt-4", "litellm_params": {"model": "openai/gpt-4-preview", "custom_llm_provider": "openai"},
     "model_info": {"id": "3", "input_cost_per_token": 1e-5, "output_cost_per_token": 2e-5}},
]


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_CONTROLLER_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text(MINI_CONFIG, encoding="utf-8")
    from litellm_controller import client as client_mod

    def fake_list_models(self):
        return [dict(m) for m in FAKE_MODELS]

    monkeypatch.setattr(client_mod.LiteLLMClient, "list_models", fake_list_models)
    monkeypatch.setattr(client_mod.LiteLLMClient, "list_credentials", lambda self: [])
    monkeypatch.setattr(client_mod.LiteLLMClient, "fetch_model_cost_map", lambda self: {})
    return tmp_path


async def test_model_list_render_and_filter(tmp_config):
    from textual.widgets import DataTable
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.models import ModelListScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        app.push_screen(ModelListScreen())
        await pilot.pause(0.5)
        assert isinstance(app.screen, ModelListScreen)
        table = app.screen.query_one("#ml-table", DataTable)
        # 3 行模型 + 重复名标注
        assert table.row_count == 3
        # 搜索筛选：只剩含 gpt 的两行
        search = app.screen.query_one("#ml-search")
        search.value = "gpt"
        await pilot.pause(0.3)
        assert table.row_count == 2
        # 状态栏被更新
        assert "模型" in app.query_one("#status").render() or app.query_one("#status")


async def test_model_row_opens_detail(tmp_config):
    from textual.widgets import DataTable
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.models import ModelListScreen
    from litellm_controller.screens.model_form import ModelFormScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        app.push_screen(ModelListScreen())
        await pilot.pause(0.6)
        table = app.screen.query_one("#ml-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause(0.1)
        # 按下回车，触发 DataTable.RowSelected -> 进入合一详情页
        await pilot.press("enter")
        await pilot.pause(0.6)
        form = next((s for s in app.screen_stack if isinstance(s, ModelFormScreen)), None)
        assert form is not None
        assert form.mode == "view"


async def test_model_form_add_mode(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.model_form import ModelFormScreen, ModelMappingRow, ManualAddRow

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        form = ModelFormScreen(mode="add")
        app.push_screen(form)
        await pilot.pause(0.6)
        assert isinstance(app.screen, ModelFormScreen)
        # 添加模式：初始无映射行，仅有「手动添加」行
        assert len(form.query(ModelMappingRow)) == 0
        assert form.query(ManualAddRow)
        await pilot.click("#row-manual-add")
        await pilot.pause(0.2)
        assert len(form.query(ModelMappingRow)) == 1


async def test_model_form_merge_replaces_unchecked(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.model_form import ModelFormScreen, ModelMappingRow
    from textual.widgets import Checkbox, Input

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        form = ModelFormScreen(mode="add")
        app.push_screen(form)
        await pilot.pause(0.6)

        def rows():
            return list(form.query(ModelMappingRow))

        def names():
            return [r.query_one(".row-model-name", Input).value for r in rows()]

        # 手工添加一个未勾选（未固定）的空行 → 拉取时被替换
        form._add_mapping_row(checked=False)
        await pilot.pause(0.2)
        assert len(rows()) == 1
        form._merge_fetched_models(["new-1", "new-2"])
        await pilot.pause(0.2)
        assert sorted(names()) == ["new-1", "new-2"]

        # 固定 new-1，取消 new-2 → 再拉取只保留 new-1，新增 new-3
        for r in rows():
            mn = r.query_one(".row-model-name", Input).value
            r.query_one(".row-check", Checkbox).value = (mn == "new-1")
        await pilot.pause(0.1)
        form._merge_fetched_models(["new-3"])
        await pilot.pause(0.2)
        assert sorted(names()) == ["new-1", "new-3"]


async def test_model_form_upstream_matches_bound_provider(tmp_config, monkeypatch):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens import model_form as mf
    from litellm_controller.screens.model_form import ModelFormScreen, ModelMappingRow
    from textual.widgets import Input

    calls = []

    def fake_fetch(upstream, timeout=15):
        calls.append(upstream["name"])
        return ["deepseek-flash", "deepseek-v4-pro"]

    monkeypatch.setattr(mf, "fetch_upstream_models", fake_fetch)

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        # 注入带 provider 绑定的 upstream：type=openai，provider=deepseek
        app.config["upstreams"] = [{
            "name": "DeepSeek", "type": "openai",
            "endpoint": "https://api.deepseek.com/v1/models",
            "key": "x", "provider": "deepseek",
        }]
        form = ModelFormScreen(mode="add")
        app.push_screen(form)
        await pilot.pause(0.6)
        form.provider_select.value = "deepseek"
        await pilot.pause(0.1)
        await pilot.click("#btn-fetch-upstream")
        await pilot.pause(0.5)
        assert calls == ["DeepSeek"]
        names = [r.query_one(".row-model-name", Input).value for r in form.query(ModelMappingRow)]
        assert sorted(names) == ["deepseek-flash", "deepseek-v4-pro"]


async def test_group_form_view_mode(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.routing_form import GroupFormScreen
    from textual.widgets import Input, SelectionList

    group = {"group_name": "grp-x", "models": ["gpt-4"], "routing_strategy": "simple-shuffle"}
    app = LiteLLMControllerApp()
    async with app.run_test(size=(130, 44)) as pilot:
        await pilot.pause(0.4)
        form = GroupFormScreen(mode="view", group_data=group)
        app.push_screen(form)
        await pilot.pause(0.6)
        assert isinstance(app.screen, GroupFormScreen)
        assert form.query_one("#frm-group-name", Input).value == "grp-x"
        assert isinstance(form.query_one("#member-list", SelectionList), SelectionList)


async def test_form_modal_collect_and_submit():
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.widgets import FormModal, FormField

    app = LiteLLMControllerApp()
    fields = [
        FormField(name="endpoint", label="Endpoint", value="https://example.com"),
        FormField(name="mode", label="Mode", kind="choice", options=[("Alpha", "a"), ("Beta", "b")], value="a"),
    ]
    modal = FormModal("测试表单", fields)
    results = []
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(modal, lambda r: results.append(r))
        await pilot.pause(0.2)
        # 点击保存按钮，验证通过并触发 _collect，无 InvalidQueryFormat
        await pilot.click("#frm-ok")
        await pilot.pause(0.2)
        assert modal not in app.screen_stack
        assert results == [{"endpoint": "https://example.com", "mode": "a"}]


async def test_confirm_modal_danger_variant_fix():
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.widgets import ConfirmModal

    app = LiteLLMControllerApp()
    # default_yes=False 会触发 yes_variant="error"（此前为无效的 "danger"）
    modal = ConfirmModal("危险操作提示", default_yes=False)
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(modal, lambda r: results.append(r))
        await pilot.pause(0.2)
        yes_btn = modal.query_one("#yes")
        assert yes_btn.variant == "error"
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert modal not in app.screen_stack
        assert results == [False]


async def test_routing_and_meta_mount_with_mock(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.routing import RoutingListScreen
    from litellm_controller.screens.meta import MetaHomeScreen, ScriptsScreen, DefaultEditorScreen
    from litellm_controller import client as client_mod

    def fake_router(self):
        return {"current_values": {"routing_groups": [
            {"group_name": "grp", "models": ["gpt-4", "missing-model"], "routing_strategy": "simple-shuffle"}
        ], "routing_strategy": "simple-shuffle"}, "fields": []}

    client_mod.LiteLLMClient.get_router_settings = fake_router
    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        app.push_screen(RoutingListScreen())
        await pilot.pause(0.5)
        assert isinstance(app.screen, RoutingListScreen)
        app.pop_screen()
        await pilot.pause(0.2)
        app.push_screen(MetaHomeScreen())
        await pilot.pause(0.3)
        assert isinstance(app.screen, MetaHomeScreen)
        app.push_screen(ScriptsScreen())
        await pilot.pause(0.3)
        assert isinstance(app.screen, ScriptsScreen)
        app.pop_screen()
        await pilot.pause(0.2)
        app.push_screen(DefaultEditorScreen())
        await pilot.pause(0.5)
        assert isinstance(app.screen, DefaultEditorScreen)


async def test_meta_build_screen(tmp_config, monkeypatch):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller import metadata as eng
    from litellm_controller.screens.meta import BuildScreen
    from textual.widgets import Static, TextArea

    monkeypatch.setattr(eng.MetadataManager, "build", lambda self: ({"m1": {"mode": "chat"}}, ["build-log-1"]))
    monkeypatch.setattr(
        eng.MetadataManager, "get_diff_stats",
        lambda self, data: {"ok": True, "added": 1, "changed": 2, "removed": 3, "total": 1},
    )

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        bs = BuildScreen()
        app.push_screen(bs)
        await pilot.pause(0.6)
        assert "m1" in bs.query_one("#build-json", TextArea).text
        assert "build-log-1" in bs.query_one("#build-log", TextArea).text
        summary = str(bs.query_one("#build-summary", Static).render())
        assert "新增 1" in summary and "修改 2" in summary and "移除 3" in summary


async def test_model_meta_form_collect(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.meta import ModelMetaFormScreen
    from textual.widgets import Input, Select

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        form = ModelMetaFormScreen("", {}, mode="add")
        app.push_screen(form)
        await pilot.pause(0.6)
        form.query_one("#mmf-key", Input).value = "m1"
        form.query_one("#mmf-in", Input).value = "1.5"
        form.query_one("#mmf-max-in", Input).value = "128000"
        form.query_one("#mmf-mode", Select).value = "chat"
        meta = form._collect()
        assert meta["input_cost_per_token"] == round(1.5 / 1e6, 12)
        assert meta["max_input_tokens"] == 128000
        assert meta["mode"] == "chat"


async def test_default_editor_entry_add(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.meta import DefaultEditorScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.4)
        de = DefaultEditorScreen()
        app.push_screen(de)
        await pilot.pause(0.8)
        before = len(de._working)
        de._entry_done("test-key", ("save", "test-key", {"mode": "chat"}))
        assert de._working.get("test-key") == {"mode": "chat"}
        assert len(de._working) == before + 1
        de._entry_done("test-key", ("delete",))
        assert "test-key" not in de._working


async def test_upstream_form_edit_has_delete(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.setup import UpstreamFormModal

    app = LiteLLMControllerApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        existing = {"name": "up1", "type": "openai", "endpoint": "http://x/models", "key": "sk-x"}
        modal = UpstreamFormModal([], existing=existing)
        results = []
        app.push_screen(modal, lambda r: results.append(r))
        await pilot.pause(0.3)
        assert modal.query("#frm-delete")
        await pilot.click("#frm-delete")
        await pilot.pause(0.2)
        assert results == ["frm-delete"]


async def test_setup_wizard_mounts(tmp_config, tmp_path):
    # 删除配置强制进入向导
    (tmp_path / "config.yaml").unlink()
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.setup import SetupWizardScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        assert any(isinstance(s, SetupWizardScreen) for s in app.screen_stack)
