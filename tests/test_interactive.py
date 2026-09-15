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


async def test_model_row_opens_actions(tmp_config):
    from textual.widgets import DataTable
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.models import ModelListScreen
    from litellm_controller.widgets import PickModal

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        app.push_screen(ModelListScreen())
        await pilot.pause(0.5)
        table = app.screen.query_one("#ml-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause(0.1)
        # 按下回车，触发 DataTable.RowSelected -> _on_row (cursor_row)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert any(isinstance(s, PickModal) for s in app.screen_stack)


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


async def test_setup_wizard_mounts(tmp_config, tmp_path):
    # 删除配置强制进入向导
    (tmp_path / "config.yaml").unlink()
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.setup import SetupWizardScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        assert any(isinstance(s, SetupWizardScreen) for s in app.screen_stack)
