"""Textual 迁移后的 headless 冒烟测试：App 启动、各屏挂载、纯逻辑层。"""
from pathlib import Path

import pytest

MINI_CONFIG = """\
litellm:
  endpoint: "http://127.0.0.1:59999"
  key: "sk-test"
upstreams: []
model_metadata:
  output_file: null
  amend_upstream:
    type: "off"
    url: null
"""


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_CONTROLLER_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text(MINI_CONFIG, encoding="utf-8")
    return tmp_path


def test_pure_layer(tmp_config):
    from litellm_controller import config as cfg
    from litellm_controller import metadata as eng
    from litellm_controller.modeldata import cost_text, model_id_of, sorted_models

    data = cfg.load_config()
    assert data["litellm"]["endpoint"].startswith("http://")
    assert cfg.SUPPORTED_UPSTREAM_TYPES == ["openai", "anthropic", "google"]

    model = {"model_name": "gpt-4", "litellm_params": {"model": "openai/gpt-4"},
             "model_info": {"id": "1", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}}
    assert model_id_of(model) == "1"
    assert cost_text(model["model_info"]) == "1/2"
    assert sorted_models([model], "public")[0]["model_name"] == "gpt-4"

    # 引擎：同步示例脚本并构建（无启用脚本时返回基础/空）
    eng.sync_example_scripts()
    meta_dir = tmp_config / "model_metadata"
    assert (meta_dir / "default.py").exists()
    mgr = eng.MetadataManager(None, data)
    result, logs = mgr.build()
    assert isinstance(result, dict)


async def test_app_boots_to_home(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.home import HomeScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        assert isinstance(app.screen, HomeScreen)
        await pilot.pause(0.2)  # 等待 HomeScreen 内部 widget 挂载
        # 主菜单项 + 状态栏
        assert app.screen.query("#home-list")
        assert app.query("#status")


async def test_all_screens_mount(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.models import ModelListScreen
    from litellm_controller.screens.routing import RoutingListScreen
    from litellm_controller.screens.settings import SettingsScreen
    from litellm_controller.screens.meta import MetaHomeScreen

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)
        for cls in (ModelListScreen, RoutingListScreen, SettingsScreen, MetaHomeScreen):
            app.push_screen(cls())
            await pilot.pause(0.3)  # 让网络 worker 启动并快速失败
            assert isinstance(app.screen, cls), f"{cls.__name__} 未能挂载"
            app.pop_screen()
            await pilot.pause(0.1)


async def test_modals_mount(tmp_config):
    from litellm_controller.app import LiteLLMControllerApp
    from litellm_controller.screens.home import HomeScreen
    from litellm_controller.widgets import (
        ConfirmModal, FormField, FormModal, InputModal, MultiPickModal,
        OutputModal, PickItem, PickModal,
    )

    app = LiteLLMControllerApp()
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(0.4)

        async def open_and_close(modal):
            app.push_screen(modal)
            await pilot.pause(0.15)
            assert app.screen is not None
            modal.dismiss(None)
            await pilot.pause(0.1)

        await open_and_close(ConfirmModal("测试确认"))
        await open_and_close(InputModal("请输入", value="abc"))
        await open_and_close(PickModal("标题", [PickItem("x", "项目X"), PickItem("y", "项目Y", disabled=True)]))
        await open_and_close(MultiPickModal("多选", [PickItem("m", "模型A"), PickItem("n", "模型B", preselected=True)]))
        await open_and_close(FormModal("表单", [
            FormField("a", "字段A", value="1"),
            FormField("t", "类型", kind="choice", value="x", options=[("X", "x"), ("Y", "y")]),
        ]))
        await open_and_close(OutputModal("输出", "hello\nworld"))


async def test_fuzzy_and_sorting():
    from litellm_controller.widgets import PickItem, filter_fuzzy, fuzzy_score

    assert fuzzy_score("", "anything") == (0, 0, 0)
    assert fuzzy_score("xyz", "abcdef") is None
    assert fuzzy_score("abc", "xxabcyy") is not None
    items = [PickItem(v, v) for v in ["openai/gpt-4", "anthropic/claude", "openai/gpt-3.5"]]
    picked = filter_fuzzy(items, "gpt", lambda i: i.label)
    assert all("gpt" in i.label for i in picked)
