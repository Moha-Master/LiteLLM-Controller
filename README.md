# LiteLLM Controller (litellmctl)

LiteLLM Controller 是一个功能强大的命令行界面（CLI）管理工具，专为 [LiteLLM Proxy](https://github.com/BerriAI/litellm) 设计，旨在简化模型管理、路由组配置以及模型元数据（定价与上下文窗口）的维护。

## 核心特性

- **模型管理**：交互式列出、添加、编辑、启用/禁用和删除 LiteLLM Proxy 中的模型。
- **路由管理**：管理模型路由组（Router Groups），支持批量清理失效模型。
- **脚本驱动的元数据管理**：
    - **优先级合并**：支持多个 Python 脚本共同生成 `model_prices_and_context_window.json`，通过优先级（Priority）实现灵活的配置覆盖。
    - **动态计费**：内置 DeepSeek 动态时段计费（高峰/谷价切换）等示例脚本。
    - **可视化编辑**：内置 `default.py` 可视化编辑器，支持从 Provider、Upstream 或手动录入批量配置模型。
- **非交互式导出**：通过 `litellmctl metadata-gen` 子命令快速构建并导出元数据 JSON 文件，便于集成到 CI/CD 流程。
- **环境适配**：支持配置文件目录自定义（`-D` 参数），自动初始化默认配置与示例脚本。

## 安装

```bash
pip install litellm-controller
```

安装后，你可以通过 `litellmctl` 命令启动工具。

## 快速开始

### 1. 初始化配置
首次运行时，工具会引导你配置 LiteLLM Proxy 的 Endpoint 和 API Key。

### 2. 管理模型元数据
进入 `3. 设置` -> `2. 模型元数据管理`：
- 使用 **可视化编辑器** 修改 `default.py`。
- 在 **脚本管理** 中启用内置的 `openrouter.py` 或 `deepseek.py` 以获取实时计费。
- 点击 **构建并导出**，生成 LiteLLM 可加载的 JSON。

### 3. 非交互式构建（CLI 模式）
如果你只想在脚本中更新定价文件：
```bash
litellmctl metadata-gen [output_directory]
```

## 元数据脚本规范

每个脚本位于 `$CONFIG_DIR/model_metadata/` 下，需包含以下头部配置：
```python
# --- LITELLMCTL CONFIG ---
NAME = "我的自定义配置"
DESCRIPTION = "描述文本"
PRIORITY = 50
ENABLED = True
# -------------------------
```
脚本需将模型字典以 JSON 格式打印至 `stdout`。

## 贡献与开发

本项目遵循 Python 3.12+ 规范，建议在虚拟环境下开发。

```bash
git clone ...
cd litellm-controller
python -m venv venv
source venv/bin/activate
pip install -e .
```

## 许可证

MIT
