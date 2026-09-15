# LiteLLM Controller (litellmctl)

LiteLLM Controller 是基于 [Textual](https://github.com/Textualize/textual) 的现代化终端用户界面（TUI）管理工具，专为 [LiteLLM Proxy](https://github.com/BerriAI/litellm) 设计，用于高效管理模型、路由组配置以及模型元数据（定价与上下文窗口）。

## 核心特性

- **现代 TUI 界面**：基于 Textual 响应式框架，支持全键盘导航、实时模糊筛选、状态栏与非阻塞异步更新。
- **模型管理**：DataTable 列表浏览、添加模型向导（5 步）、就地编辑表单、一键启用/禁用与删除。
- **路由管理**：路由组（Router Groups）管理、多步添加/编辑向导、一键扫描并清理失效成员模型。
- **脚本驱动的元数据管理**：
    - **优先级合并**：支持多个 Python 脚本共同生成 `model_prices_and_context_window.json`，通过优先级（Priority）实现多源配置覆盖。
    - **可视化编辑**：内置 `default.py` 完整可视化编辑器，支持从 Provider、Upstream 或手动录入批量配置模型、阶梯定价与高级特性。
    - **参数预览对比**：实时与 LiteLLM Proxy 线上参数比对（新增/变更/相同状态标注）。
- **CI/CD 非交互导出**：通过 `litellmctl metadata-gen` 子命令直接生成定价 JSON，便于集成自动化流程。

## 安装

```bash
pip install litellm-controller
```

安装后即可通过 `litellmctl` 命令启动 TUI。

## 快捷键与操作

| 场景 | 快捷键 | 作用 |
|---|---|---|
| **全局** | `Ctrl+Q` | 退出程序 |
| **主菜单** | `1` / `2` / `3` | 直达模型管理 / 路由管理 / 设置 |
| | `q` | 退出 |
| **列表类页面** | `↑` / `↓` | 移动光标 |
| | `回车` | 查看 / 操作所选行 |
| | `/` | 聚焦搜索框（输入实时模糊过滤） |
| | `r` | 刷新数据 |
| | `Esc` | 返回上一级 |
| **模态弹窗** | `Esc` | 取消并关闭 |
| **多选列表** | `空格` | 勾选 / 取消勾选 |

## 快速开始

### 1. 初始化配置
首次启动时，向导会自动引导配置 LiteLLM Proxy 的 Endpoint、Master Key 以及可选的 Upstream。

### 2. 交互式使用
```bash
litellmctl              # 使用默认配置 ~/.config/litellm-controller
litellmctl -D /path/dir # 指定配置文件目录
```

### 3. 非交互式构建（CLI 模式）
若只想在脚本中静默更新定价文件：
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
脚本需将模型字典以 JSON 格式打印至 `stdout`，日志信息打印至 `stderr`。

## 开发

本项目遵循 Python 3.10+ 规范。

```bash
git clone https://github.com/Moha-Master/LiteLLM-Controller.git
cd LiteLLM-Controller
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pytest
```

## 许可证

MIT
