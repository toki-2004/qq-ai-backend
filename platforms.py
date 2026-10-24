# -*- coding: utf-8 -*-
"""API 平台余额查询适配器：每个平台实现 fetch(api_key) -> (总余额 float, 币种 str)。"""

import requests


def _request(url, api_key):
    resp = requests.get(
        url,
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError("HTTP %d" % resp.status_code)
    return resp.json()


def _deepseek(api_key):
    data = _request("https://api.deepseek.com/user/balance", api_key)
    infos = data.get("balance_infos") or []
    total = sum(float(i.get("total_balance") or 0) for i in infos)
    return total, "CNY"


def _moonshot(api_key):
    data = _request("https://api.moonshot.cn/v1/users/me/balance", api_key)
    info = data.get("data") or {}
    total = float(info.get("available_balance") or info.get("cash_balance") or 0)
    return total, "CNY"


def _siliconflow(api_key):
    data = _request("https://api.siliconflow.cn/v1/user/info", api_key)
    info = data.get("data") or {}
    total = float(info.get("total_balance") or info.get("balance") or 0)
    return total, "CNY"


class Provider:
    def __init__(self, pid, name, docs_url, fetch):
        self.pid = pid
        self.name = name
        self.docs_url = docs_url
        self.fetch = fetch


PROVIDERS = {
    "deepseek": Provider(
        "deepseek", "DeepSeek", "https://platform.deepseek.com/", _deepseek
    ),
    "moonshot": Provider(
        "moonshot", "Kimi (Moonshot)", "https://platform.moonshot.cn/", _moonshot
    ),
    "siliconflow": Provider(
        "siliconflow", "SiliconFlow 硅基流动", "https://cloud.siliconflow.cn/",
        _siliconflow,
    ),
}
