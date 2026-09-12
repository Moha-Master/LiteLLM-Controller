"""LiteLLM API 客户端封装。"""
import requests


class LiteLLMError(Exception):
    pass


class LiteLLMClient:
    def __init__(self, endpoint: str, key: str, timeout: int = 30):
        self.base_url = endpoint.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs):
        url = self.base_url + path
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise LiteLLMError(f"无法连接 LiteLLM: {e}") from e
        if resp.status_code >= 400:
            raise LiteLLMError(f"请求失败 (HTTP {resp.status_code}): {self._extract_detail(resp)}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise LiteLLMError("响应不是有效的 JSON") from e

    @staticmethod
    def _extract_detail(resp) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text[:200]
        if isinstance(data, dict):
            for k in ("detail", "message"):
                v = data.get(k)
                if isinstance(v, str):
                    return v[:300]
            error = data.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"][:300]
            if isinstance(error, str):
                return error[:300]
            return str(data)[:300]
        return str(data)[:300]

    def list_models(self) -> list:
        data = self._request("GET", "/model/info")
        return data.get("data", [])

    def create_model(self, model_name: str, litellm_params: dict, model_info: dict = None):
        body = {
            "model_name": model_name,
            "litellm_params": litellm_params,
            "model_info": model_info or {},
        }
        return self._request("POST", "/model/new", json=body)

    def update_model(self, model_id: str, **fields):
        body = {k: v for k, v in fields.items() if v is not None}
        return self._request("PATCH", f"/model/{model_id}/update", json=body)

    def delete_model(self, model_id: str):
        return self._request("POST", "/model/delete", json={"id": model_id})

    def list_credentials(self) -> list:
        data = self._request("GET", "/credentials")
        return data.get("credentials", [])

    def get_router_settings(self) -> dict:
        """GET /router/settings：返回 fields / current_values / routing_strategy_descriptions。"""
        return self._request("GET", "/router/settings")

    def update_router_settings(self, router_settings: dict):
        """POST /config/update：写入 router_settings（浅合并，routing_groups 需整表提交）。"""
        return self._request("POST", "/config/update", json={"router_settings": router_settings})

    def fetch_model_cost_map(self) -> dict:
        data = self._request("GET", "/public/litellm_model_cost_map")
        return data.get("litellm_model_cost_map", data)
