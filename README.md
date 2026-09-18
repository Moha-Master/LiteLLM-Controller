# LiteLLM Controller (litellmctl)

LiteLLM Controller 是基于 [Textual](https://github.com/Textualize/textual) 的现代化终端用户界面（TUI）管理工具，专为 [LiteLLM Proxy](https://github.com/BerriAI/litellm) 设计，用于高效管理模型、路由组配置以及模型元数据（定价与上下文窗口）。

## 核心特性

- **现代 TUI 界面**：基于 Textual 8.x 响应式框架，统一页面骨架（顶栏 / 内容区 / 底部提示栏 / ⋮ 折叠菜单）、全键盘导航 + 单击即执行鼠标操作、三段式模态、实时模糊筛选、状态栏与非阻塞异步更新。
- **合一式模型表单**：添加、查看（只读）、编辑复用同一页面，单窗口内完成 Provider 选择、模型映射（支持从 LiteLLM 价格表 / Upstream 一键拉取并按勾选替换）、Credential 绑定与高级选项（阶梯价格、order/weight、自定义 JSON），无需多步向导。
- **合一式路由组表单**：创建、查看、编辑同样复用单页；组名 / 路由策略 / 成员模型实时筛选勾选，支持一键清理失效成员。
- **脚本驱动的元数据管理**：
    - **一窗构建预览**：单个界面内完成构建，大文本框预览生成的 JSON、小文本框展示构建日志，底部实时给出「新增 / 修改 / 移除 / 共计」与 Proxy 的对比概览，并一键导出。
    - **优先级合并**：支持多个 Python 脚本共同生成 `model_prices_and_context_window.json`，通过优先级（Priority）实现多源配置覆盖；脚本管理为「一窗设置」：单击脚本行弹出表单统一维护名称、描述、优先级、启用开关与脚本内容，支持搜索、排序与新建。
    - **可视化编辑**：`default.py` 完整可视化编辑器，单窗口内配置价格、上下文、Mode、特性开关、阶梯定价与自定义字段，支持新增/编辑复用同一表单。
- **CI/CD 非交互导出**：通过 `litellmctl metadata-gen` 子命令直接生成定价 JSON，便于集成自动化流程。

## 安装

```bash
pip install litellm-controller
```

安装后即可通过 `litellmctl` 命令启动 TUI。

## 快捷键与操作

与 aliyunctl / clashctl 共享同一套界面规范（见 `AGENTS.md` 第一部分）。

| 场景 | 快捷键 | 作用 |
|---|---|---|
| **全局** | `Ctrl+Q` | 任意界面退出程序 |
| **主菜单** | `1` / `2` / `3` / `4` | 直达模型管理 / 路由管理 / 设置 / 退出 |
| | `Esc` / `Ctrl+C` | 退出程序 |
| **功能页** | `Esc` / `Ctrl+C` | 返回上一级（有未保存修改时先确认） |
| | `Ctrl+R` | 刷新当前页数据 |
| | `Home` / `End` / `PgUp` / `PgDn` | 页面滚动 |
| **列表页** | `↑` / `↓`、`回车` | 移动光标、执行所选行 |
| | 鼠标 | **单击数据行即执行**（查看 / 编辑 / 启停）；滚轮滚动 |
| | `/` | 聚焦搜索框（输入实时筛选；编辑态自动禁用 `Ctrl+R`） |
| | `Ctrl+N` | 新增条目（模型 / 路由组 / Upstream / 参数） |
| | `Ctrl+E` / `Ctrl+D` | 设置页：编辑 / 删除 Upstream；脚本页：调优先级 |
| **表单 / 模态** | `Tab` / `Shift+Tab` | 轮切字段 |
| | `Enter` | 提交（FormModal / InputModal；sheet 大表单用按钮提交） |
| | `Esc` / `Ctrl+C` | 取消并关闭 |
| **折叠菜单 ⋮** | 单击 `菜单 ▾` / 点击外部 / `Esc` | 开合（低频操作：重置配置等） |
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
