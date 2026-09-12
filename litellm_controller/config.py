"""配置层：配置文件加载/保存与交互式配置向导。"""
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from InquirerPy import prompt
from InquirerPy.base.control import Choice

from . import ui  # noqa: F401  导入即注册 DotFuzzy
from .client import LiteLLMClient
from .ui import FuzzySeparator
from .upstreams import UPSTREAM_TYPES

CONFIG_DIR_ENV = "LITELLM_CONTROLLER_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "~/.config/litellm-controller"

SUPPORTED_UPSTREAM_TYPES = ["openai", "anthropic", "google"]


def get_config_dir() -> Path:
    config_dir = os.environ.get(
        CONFIG_DIR_ENV,
        DEFAULT_CONFIG_DIR,
    )
    return Path(config_dir).expanduser()


def _get_config_path() -> Path:
    return get_config_dir() / "config.yaml"


def get_default_model_metadata() -> dict:
    return {
        "output_file": None,
        "amend_upstream": {
            "type": "off",
            "url": None,
        },
    }


def cost_map_providers(cost_map: dict) -> list:
    """从 cost map 提取内置 provider 名称列表（过滤非常规条目）。"""
    providers = set()
    for value in cost_map.values():
        if not isinstance(value, dict):
            continue
        provider = value.get("litellm_provider")
        if not isinstance(provider, str):
            continue
        if " " in provider or "http" in provider or provider.startswith("text-completion-"):
            continue
        providers.add(provider)
    return sorted(providers)


def fetch_known_providers(endpoint: str, key: str) -> list:
    """尽力从 LiteLLM 拉取内置 provider 列表；失败返回空列表（回退手动输入）。"""
    try:
        client = LiteLLMClient(endpoint, key, timeout=10)
        cost_map = client.fetch_model_cost_map()
    except Exception:
        return []
    return cost_map_providers(cost_map)


def guess_provider_from_name(name: str, known_providers: list) -> str | None:
    """根据 Upstream 名称猜测 Provider：归一化（小写、去非字母数字）后做子串匹配。

    匹配串至少 3 个字符（避免 "p" 之类短名误伤）；多候选取最长。"""
    norm_name = "".join(ch for ch in (name or "").lower() if ch.isalnum())
    if not norm_name:
        return None
    candidates = []
    for provider in known_providers or []:
        norm_provider = "".join(ch for ch in provider.lower() if ch.isalnum())
        if len(norm_provider) >= 3 and norm_provider in norm_name:
            candidates.append(provider)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=len)


def validate_model_metadata(meta: dict) -> dict:
    if not isinstance(meta, dict):
        raise ValueError("model_metadata 必须是映射")

    # 未知配置项（如历史遗留的 provider 段）静默忽略，不参与任何逻辑

    # amend_upstream
    amend = meta.get("amend_upstream")
    if amend is None:
        meta["amend_upstream"] = {"type": "off", "url": None}
    elif not isinstance(amend, dict):
        raise ValueError("model_metadata.amend_upstream 必须是映射")
    else:
        a_type = amend.get("type", "off")
        if a_type not in ("off", "url", "file"):
            raise ValueError(f"amend_upstream.type 不受支持: {a_type} (可选: off, url, file)")
        if a_type == "url":
            u = (amend.get("url") or "").strip()
            if not u.startswith(("http://", "https://")):
                raise ValueError("amend_upstream.url 必须是有效 http(s) URL")

    return meta


def load_config() -> dict:
    config_path = _get_config_path()
    if not config_path.exists():
        raise ValueError(f"配置文件不存在: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("配置文件格式错误: 顶层必须是映射")
    litellm = data.get("litellm")
    if not isinstance(litellm, dict):
        raise ValueError("配置缺少 litellm 段")
    for field in ("endpoint", "key"):
        value = litellm.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"litellm.{field} 缺失或为空")
    upstreams = data.get("upstreams")
    if upstreams is None:
        data["upstreams"] = []
    elif not isinstance(upstreams, list):
        raise ValueError("upstreams 必须是列表")
    for i, up in enumerate(data["upstreams"]):
        if not isinstance(up, dict):
            raise ValueError(f"upstreams[{i}] 必须是映射")
        for field in ("name", "type", "endpoint", "key"):
            value = up.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"upstreams[{i}].{field} 缺失或为空")
        if up["type"] not in SUPPORTED_UPSTREAM_TYPES:
            raise ValueError(f"upstreams[{i}].type 不受支持: {up['type']}")
        provider = up.get("provider")
        if provider is None or (isinstance(provider, str) and not provider.strip()):
            up.pop("provider", None)
        else:
            if not isinstance(provider, str):
                raise ValueError(f"upstreams[{i}].provider 设置时必须是字符串")
            up["provider"] = provider.strip()
        endpoint = up["endpoint"].strip()
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(f"upstreams[{i}].endpoint 必须是 http(s) URL")
        if not urlparse(endpoint).path.strip("/"):
            example = UPSTREAM_TYPES[up["type"]]["example_url"]
            raise ValueError(
                f"upstreams[{i}].endpoint 应为完整的模型列表 API URL（当前缺少路径），如 {example}"
            )
    
    # model_metadata
    if "model_metadata" not in data:
        data["model_metadata"] = get_default_model_metadata()
    else:
        data["model_metadata"] = validate_model_metadata(data["model_metadata"])

    return data


def save_config(data: dict) -> Path:
    config_path = _get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return config_path


_save_config = save_config


def _ask_litellm_section():
    return prompt(
        [
            {
                "type": "input",
                "message": "请输入 LiteLLM endpoint（如 https://llm.example.com）:",
                "name": "endpoint",
                "validate": lambda val: bool(val and val.strip()),
                "invalid_message": "endpoint 不能为空",
            },
            {
                "type": "password",
                "message": "请输入 LiteLLM key（master key）:",
                "name": "key",
                "validate": lambda val: bool(val and val.strip()),
                "invalid_message": "key 不能为空",
            },
        ]
    )


def ask_provider_binding(endpoint: str = None, key: str = None, default: str = None,
                         known_providers: list = None, preselect: str = None):
    """交互式选择 Provider 绑定。

    preselect 非空且在已知列表中时，该 provider 置顶并标记（指针初始落在其上，
    搜索框保持空、全部选项可见）。
    返回 (canceled: bool, provider: str | None)；provider 为 None 表示不绑定。"""
    if known_providers is None:
        known_providers = fetch_known_providers(endpoint, key) if endpoint and key else []
    if known_providers:
        if preselect and preselect not in known_providers:
            preselect = None
        if preselect:
            ordered = [preselect] + [p for p in known_providers if p != preselect]
        else:
            ordered = list(known_providers)
        choices = [FuzzySeparator("─" * 50)]
        choices.extend(
            Choice(p, name=f"{p}  · 按名称推荐" if p == preselect else p)
            for p in ordered
        )
        choices.append(FuzzySeparator("─" * 50))
        choices.append(Choice("__custom__", name="[手动输入其他 Provider]"))
        choices.append(Choice("__none__", name="[不绑定]"))
        message = (
            f"选择绑定的 LiteLLM Provider（已从 LiteLLM 获取 {len(known_providers)} 个，"
            "留空不绑定）:"
        )
        if preselect:
            message = (
                f"选择绑定的 LiteLLM Provider（已按 Upstream 名称推荐 {preselect}）:"
            )
        try:
            provider_answer = prompt(
                [
                    {
                        "type": "fuzzy",
                        "message": message,
                        "name": "provider",
                        "choices": choices,
                        "long_instruction": "输入筛选 · ↑↓ 移动 · 回车 确认 · Ctrl+C 取消",
                    }
                ]
            )
        except (KeyboardInterrupt, EOFError):
            return True, None
        if not provider_answer:
            return True, None
        provider = provider_answer.get("provider")
        if provider == "__none__":
            return False, None
        if provider == "__custom__":
            try:
                custom_answer = prompt(
                    [
                        {
                            "type": "input",
                            "message": "请输入 Provider 名称 (如 my-gateway):",
                            "name": "provider",
                            "validate": lambda val: bool(val and val.strip()),
                            "invalid_message": "provider 不能为空",
                        }
                    ]
                )
            except (KeyboardInterrupt, EOFError):
                return True, None
            if not custom_answer:
                return True, None
            return False, (custom_answer.get("provider") or "").strip() or None
        return False, provider
    try:
        provider_answer = prompt(
            [
                {
                    "type": "input",
                    "message": (
                        "请输入绑定的 LiteLLM Provider 名称（留空表示不绑定，"
                        "如 openrouter / openai / p）:"
                    ),
                    "name": "provider",
                    "default": default or "",
                    "long_instruction": "绑定后可在添加模型流程与元数据管理中自动推荐此 Upstream",
                }
            ]
        )
    except (KeyboardInterrupt, EOFError):
        return True, None
    if not provider_answer:
        return True, None
    return False, (provider_answer.get("provider") or "").strip() or None


def ask_upstream_fields(endpoint: str = None, key: str = None, existing: dict = None,
                        known_providers: list = None):
    """交互式采集单个 Upstream 配置；传入 existing 时预填当前值（用于编辑）。

    任一步骤取消返回 None，否则返回 upstream dict。"""
    existing = existing or {}
    try:
        name_answer = prompt(
            [
                {
                    "type": "input",
                    "message": "请输入 Upstream 名称（如 anthropic-main，仅用于显示）:",
                    "name": "name",
                    "default": existing.get("name", ""),
                    "validate": lambda val: bool(val and val.strip()),
                    "invalid_message": "名称不能为空",
                }
            ]
        )
        if not name_answer:
            return None
        type_answer = prompt(
            [
                {
                    "type": "list",
                    "message": "请选择 Upstream 类型:",
                    "name": "type",
                    "choices": SUPPORTED_UPSTREAM_TYPES,
                    "default": existing.get("type"),
                }
            ]
        )
        if not type_answer:
            return None
        endpoint_answer = prompt(
            [
                {
                    "type": "input",
                    "message": (
                        f"请输入 Upstream 模型列表 API 的完整 URL"
                        f"（如 {UPSTREAM_TYPES[type_answer['type']]['example_url']}）:"
                    ),
                    "name": "endpoint",
                    "default": existing.get("endpoint", ""),
                    "validate": lambda val: bool(
                        val and val.strip() and val.strip().startswith(("http://", "https://"))
                    ),
                    "invalid_message": "请输入完整的 http(s) URL",
                }
            ]
        )
        if endpoint_answer is None:
            return None
        has_existing_key = bool(existing.get("key"))
        key_answer = prompt(
            [
                {
                    "type": "password",
                    "message": (
                        "请输入 Upstream key（留空保持原 key）:"
                        if has_existing_key
                        else "请输入 Upstream key:"
                    ),
                    "name": "key",
                    "validate": (lambda val: True)
                    if has_existing_key
                    else (lambda val: bool(val and val.strip())),
                    "invalid_message": "key 不能为空",
                }
            ]
        )
        if key_answer is None:
            return None
        key_value = (key_answer.get("key") or "").strip() or existing.get("key", "")
        preselect = (
            guess_provider_from_name(name_answer["name"], known_providers)
            if known_providers
            else None
        )
        canceled, provider = ask_provider_binding(
            endpoint, key, default=existing.get("provider"),
            known_providers=known_providers, preselect=preselect,
        )
        if canceled:
            return None
        upstream = {
            "name": name_answer["name"].strip(),
            "type": type_answer["type"],
            "endpoint": (endpoint_answer.get("endpoint") or "").strip(),
            "key": key_value,
        }
        if provider:
            upstream["provider"] = provider
        return upstream
    except (KeyboardInterrupt, EOFError):
        return None


def _ask_upstreams(endpoint: str = None, key: str = None):
    upstreams = []
    known_providers = fetch_known_providers(endpoint, key) if endpoint and key else []
    if endpoint and key and not known_providers:
        print("未能从 LiteLLM 获取内置 Provider 列表，绑定 Provider 将使用手动输入。")
    while True:
        upstream = ask_upstream_fields(endpoint, key, known_providers=known_providers)
        if upstream is None:
            return None
        upstreams.append(upstream)
        more_answer = prompt(
            [
                {
                    "type": "confirm",
                    "message": "继续添加另一个 Upstream?",
                    "name": "more",
                    "default": False,
                }
            ]
        )
        if not more_answer or not more_answer.get("more"):
            return upstreams


def _run_setup_flow() -> bool:
    try:
        answers = _ask_litellm_section()
        if not answers:
            print("\n配置已取消。")
            return False
        upstreams = _ask_upstreams(
            answers["endpoint"].strip(), answers["key"]
        )
        if upstreams is None:
            print("\n配置已取消。")
            return False
    except (KeyboardInterrupt, EOFError):
        print("\n配置已取消。")
        return False
    data = {
        "litellm": {
            "endpoint": answers["endpoint"].strip(),
            "key": answers["key"],
        },
        "upstreams": upstreams,
    }

    print("\n======== 最终配置概览 ========")
    print(f"LiteLLM Endpoint: {data['litellm']['endpoint']}")
    print(f"LiteLLM Key     : {'***' if data['litellm']['key'] else '空'}")
    print(f"Upstreams       : {len(data['upstreams'])} 个")
    for i, u in enumerate(data["upstreams"], 1):
        print(f"  {i}. {u['name']} ({u['type']})")
    print("=" * 30)

    try:
        conf = prompt(
            [{"type": "confirm", "message": "是否确认以上配置并保存？", "name": "ok", "default": True}]
        )
    except (KeyboardInterrupt, EOFError):
        print("\n配置已取消。")
        return False

    if not conf or not conf.get("ok"):
        print("\n配置已取消，未保存。")
        return False

    path = _save_config(data)
    print(f"配置已保存到: {path}")
    return True


def ensure_config_ready() -> bool:
    config_path = _get_config_path()
    if not config_path.exists():
        print(f"未找到配置文件: {config_path}")
        print("现在进入交互式配置流程。")
        return _run_setup_flow()
    try:
        load_config()
        return True
    except Exception as e:
        print(f"配置文件损坏或不可读取: {e}")
    try:
        answers = prompt(
            [
                {
                    "type": "list",
                    "message": "请选择操作:",
                    "name": "choice",
                    "choices": [
                        {"name": "重新配置并覆盖", "value": "reset"},
                        {"name": "退出程序", "value": "exit"},
                    ],
                }
            ]
        )
    except KeyboardInterrupt:
        return False
    if not answers or answers.get("choice") != "reset":
        return False
    return _run_setup_flow()
