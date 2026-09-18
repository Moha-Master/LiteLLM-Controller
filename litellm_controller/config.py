"""配置层：配置文件的加载/保存与纯数据校验（交互向导已迁移至 screens/setup.py）。"""
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .client import LiteLLMClient
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


def get_config_path() -> Path:
    return get_config_dir() / "config.yaml"


def config_exists() -> bool:
    return get_config_path().exists()


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
    config_path = get_config_path()
    if not config_path.exists():
        raise ValueError(f"配置文件不存在: {config_path}")
    with open(config_path, encoding="utf-8") as f:
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
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return config_path
