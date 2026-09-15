"""模型数据的纯函数层：格式化、排序、cost map 查询（无 UI 依赖）。"""
from collections import Counter


def model_id_of(model: dict) -> str:
    return (model.get("model_info") or {}).get("id") or model.get("model_name")


def fmt_cost(per_token) -> str:
    if per_token is None:
        return "-"
    return f"{per_token * 1e6:.4g}"


def cost_text(info: dict) -> str:
    return f"{fmt_cost(info.get('input_cost_per_token'))}/{fmt_cost(info.get('output_cost_per_token'))}"


def cost_map_models(cost_map: dict, provider: str) -> list:
    """cost map 中属于指定 provider 的模型名列表。"""
    return sorted(
        key
        for key, value in cost_map.items()
        if isinstance(value, dict) and value.get("litellm_provider") == provider
    )


def credential_display(credentials: list) -> list:
    """Credential 列表 → [{name, provider, key}]（key 为 API 返回的掩码值）。"""
    rows = []
    for c in credentials:
        info_c = c.get("credential_info") or {}
        values_c = c.get("credential_values") or {}
        rows.append(
            {
                "name": str(c.get("credential_name", "?")),
                "provider": str(info_c.get("custom_llm_provider", "?")),
                "key": str(values_c.get("api_key", "") or ""),
            }
        )
    return rows


def sorted_models(models: list, sort_key: str) -> list:
    def key(m):
        params = m.get("litellm_params") or {}
        info = m.get("model_info") or {}
        if sort_key == "litellm":
            return (params.get("model") or "",)
        if sort_key == "provider":
            return (params.get("custom_llm_provider") or "", m.get("model_name", ""))
        if sort_key == "credential":
            return (params.get("litellm_credential_name") or "", m.get("model_name", ""))
        if sort_key == "status":
            return (bool(info.get("blocked", False)), m.get("model_name", ""))
        return (m.get("model_name", ""),)

    return sorted(models, key=key)


def duplicate_name_counts(models: list) -> Counter:
    return Counter(m.get("model_name", "?") for m in models)
