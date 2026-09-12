"""Upstream 类型注册表与上游模型列表拉取。

endpoint 必须填写完整的模型列表 API URL，不做任何拼接，例如：
  openai:    https://api.openai.com/v1/models
  anthropic: https://api.anthropic.com/v1/models
  google:    https://generativelanguage.googleapis.com/v1beta/models
"""
from urllib.parse import urlparse

import requests


class UpstreamError(Exception):
    pass


MAX_PAGE_SIZE = 1000

UPSTREAM_TYPES = {
    "openai": {
        "label": "OpenAI",
        "example_url": "https://api.openai.com/v1/models",
        "auth": "bearer",
    },
    "anthropic": {
        "label": "Anthropic",
        "example_url": "https://api.anthropic.com/v1/models",
        "auth": "x-api-key",
    },
    "google": {
        "label": "Google",
        "example_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "auth": "query-key",
    },
}


def upstream_label(upstream: dict) -> str:
    spec = UPSTREAM_TYPES.get(upstream.get("type"), {})
    return spec.get("label", upstream.get("type", "?"))


def fetch_upstream_models(upstream: dict, timeout: int = 30) -> list:
    spec = UPSTREAM_TYPES.get(upstream.get("type"))
    if spec is None:
        raise UpstreamError(f"不支持的 Upstream 类型: {upstream.get('type')}")
    url = (upstream.get("endpoint") or "").strip()
    if not url:
        raise UpstreamError(
            f"Upstream [{upstream['name']}] endpoint 未填写（应为完整的模型列表 API URL，如 {spec['example_url']}）"
        )
    if not urlparse(url).path.strip("/"):
        print(
            f"警告: Upstream [{upstream['name']}] endpoint 看起来缺少路径，"
            f"应填写完整的模型列表 API URL，如 {spec['example_url']}"
        )
    key = upstream["key"]
    headers = {}
    params = {}
    if spec["auth"] == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif spec["auth"] == "x-api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        params["limit"] = MAX_PAGE_SIZE
    else:  # query-key
        params["key"] = key
        params["pageSize"] = MAX_PAGE_SIZE

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise UpstreamError(f"连接 Upstream [{upstream['name']}] 失败: {e}") from e
    if resp.status_code != 200:
        raise UpstreamError(
            f"Upstream [{upstream['name']}] 返回 HTTP {resp.status_code} (请求 {url}): {resp.text[:200]}"
        )
    try:
        payload = resp.json()
    except ValueError as e:
        raise UpstreamError(f"Upstream [{upstream['name']}] 返回了非 JSON 数据") from e

    if spec["auth"] == "query-key":
        models = [
            m["name"].split("/")[-1]
            for m in payload.get("models", [])
            if isinstance(m, dict) and m.get("name")
        ]
        truncated = bool(payload.get("nextPageToken"))
    else:
        models = [m["id"] for m in payload.get("data", []) if isinstance(m, dict) and m.get("id")]
        truncated = bool(payload.get("has_more"))
    if truncated:
        print(f"警告: Upstream [{upstream['name']}] 模型数量超过 {MAX_PAGE_SIZE} 个，列表可能被截断。")
    return models
