# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Agent 与共享 KV（RADAR_KV）的唯一通道。

架构约束（2026-11-12 定）：三个项目之间只允许「存储介质依赖」，不允许「服务接口依赖」。
radar / console / agent 共用同一个 Cloudflare KV 命名空间（radar 侧叫 RADAR_KV），所以
agent 直接经 Cloudflare KV REST API 读写这个 KV，既不调 console 的 /api/hotwords，
也不调 radar 的 /api/serp、/api/trend、/api/candidates——这些 App 接口一律不碰。

键名与 console/radar 完全对齐（见下方常量 + term_key），写侧口径在 curation.py 复刻
store.js，保证 agent 直写 KV 与 console 人工操作落在同一套键、同一套状态枚举上。

transport 可注入：生产走 requests 打 Cloudflare；测试注入内存实现（见 test/ 里的 FakeKV），
不联网、不依赖 Cloudflare 凭据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)

# ---- 键前缀 / 键名：与 console/src/store.js、radar/src/store.js 严格对齐 ----
AI_CACHE_PREFIX = "ai:v3:"          # AI 站点方案正文（无 TTL）
SERP_PREFIX = "serp:v1:"            # 某词真实 SERP 前 10 + 竞争度（TTL 7 天）
TREND_PREFIX = "trend:v1:"          # 某词 30 天趋势曲线（TTL 3 天）
HINTS_KEY = "aihints"               # 词 → 信号标签覆盖（人工 / AI 修正）
CANDIDATES_KEY = "candidates"       # radar 重建的候选全表
SAVED_KEY = "saved"                 # 收藏 / 跟进表（含 status）
REJECTED_KEY = "rejected"           # 「不做」表
TAGS_KEY = "tags"                   # 词 → 标签
COMPETITION_KEY = "competition"     # 词 → {score, level, at} 摘要表
INTERESTS_KEY = "interests"         # 词 → {term, summary, points} 趋势摘要表

_WS = re.compile(r"\s+")

# 与 console/src/store.js 的 termKey 完全一致：小写 + 折叠空白
def term_key(t: str) -> str:
    return _WS.sub(" ", str(t or "").lower()).strip()


def _api_base(account_id: str, namespace_id: str) -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/storage/kv/namespaces/{namespace_id}"
    )


class CloudflareKV:
    """Cloudflare KV REST 客户端。transport 为 None 时走 requests 打真实 API。

    transport 签名：(method: str, url: str, headers: dict, body: Optional[str]) -> (status: int, text: str)
    注入假 transport 即可在单测里完全离线验证读写逻辑。
    """

    def __init__(
        self,
        account_id: str,
        namespace_id: str,
        api_token: str,
        transport: Optional[Callable[[str, str, dict, Optional[str]], tuple[int, str]]] = None,
    ) -> None:
        self.account_id = account_id
        self.namespace_id = namespace_id
        self.api_token = api_token
        self.transport = transport

    @classmethod
    def from_env(cls, transport=None) -> "CloudflareKV":
        return cls(
            account_id=os.environ.get("CF_ACCOUNT_ID", ""),
            namespace_id=os.environ.get("KV_NAMESPACE_ID", ""),
            api_token=os.environ.get("CF_API_TOKEN", ""),
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.namespace_id and self.api_token)

    # ---- 底层请求 ----
    def _request(self, method: str, key: Optional[str], *, params=None, body=None) -> tuple[int, str]:
        # ★键名必须百分号编码：我们的键是 "ai:v3:free invoice calculator" 这种含冒号/空格/可能含
        #   # ? % 的自然语言词组，裸拼进路径会被 URL 解析截断或改写（Cloudflare 文档明确要求编码）。
        if key is not None:
            url = _api_base(self.account_id, self.namespace_id) + "/values/" + urllib.parse.quote(key, safe="")
        else:
            url = _api_base(self.account_id, self.namespace_id) + "/keys"

        # __list__ 是内部标记（区分 /keys 与 /values），绝不能发给 Cloudflare
        q = {k: v for k, v in (params or {}).items() if k != "__list__"} or None

        if self.transport is not None:
            full = url + ("?" + urllib.parse.urlencode(q) if q else "")
            headers = {"Authorization": "Bearer " + self.api_token}
            return self.transport(method, full, headers, body)

        # 生产路径：真实打 Cloudflare
        headers = {"Authorization": "Bearer " + self.api_token}
        if body is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
        try:
            if key is not None:
                resp = requests.request(method, url, params=q, headers=headers, data=body, timeout=30)
            else:
                resp = requests.get(url, params=q, headers=headers, timeout=30)
        except requests.RequestException as exc:  # 网络层失败：fail-open
            return 502, str(exc)
        return resp.status_code, resp.text

    # ---- 读 ----
    def get_text(self, key: str, default: Optional[str] = None) -> Optional[str]:
        status, text = self._request("GET", key)
        if status == 200 and text != "":
            return text
        # 404 = 键不存在，正常；其余（401 凭据错、403 权限、5xx）必须留痕，
        # 否则「读不到」会伪装成「数据是空的」，排查时完全看不出方向。
        if status not in (200, 404):
            log.warning("KV 读取异常 %s: status=%s %s", key, status, str(text)[:120])
        return default

    def get_json(self, key: str, default: Any = None) -> Any:
        text = self.get_text(key)
        if text is None:
            return default
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return default

    # ---- 写 ----
    def put_text(self, key: str, text: str, ttl: Optional[int] = None) -> bool:
        """ttl 单位秒。★REST 的查询参数名是 `expiration_ttl`（snake_case，最小 60），
        不是 Workers binding 的 `expirationTtl`——发错名字会被忽略或报 400，
        结果要么键永不过期、要么写入直接失败。"""
        if not self.configured:
            return False
        params = None
        if ttl:
            params = {"expiration_ttl": max(60, int(ttl))}
        status, _ = self._request("PUT", key, params=params, body=text)
        return status in (200, 201)

    def put_json(self, key: str, obj: Any, ttl: Optional[int] = None) -> bool:
        try:
            text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        return self.put_text(key, text, ttl=ttl)

    # ---- 列举 ----
    def list_keys(self, prefix: str) -> list[str]:
        """返回所有以 prefix 开头的完整键名（含前缀）。未配置或失败返回空列表。

        ★Cloudflare KV REST 的真实响应是：
            {"result": [{"name": "..."}], "result_info": {"count": 1, "cursor": "..."}}
        即 result 是**数组**、游标在 result_info 里（早期版本曾按 {"result":{"keys":[...]}}
        写，结果线上直接 "'list' object has no attribute 'get'"）。两种都兼容。
        """
        if not self.configured and self.transport is None:
            return []
        out: list[str] = []
        cursor = None
        for _ in range(50):  # 防御性上限，正常远不会到
            params = {"prefix": prefix, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            status, text = self._request("GET", None, params={**params, "__list__": True})
            if status != 200:
                break
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                break
            if not isinstance(data, dict):
                break
            result = data.get("result")
            if isinstance(result, dict):          # 兜底：老式 {"result": {"keys": [...]}}
                keys = result.get("keys") or []
                cursor = result.get("cursor") or None
            elif isinstance(result, list):        # 真实 Cloudflare 格式
                keys = result
                info = data.get("result_info")
                cursor = (info or {}).get("cursor") if isinstance(info, dict) else None
            else:
                break
            for k in keys:
                name = k.get("name") if isinstance(k, dict) else k
                if name:
                    out.append(str(name))
            if not cursor:
                break
        return out


# 模块级单例：生产路径默认从这里取；单测显式传 kv 覆盖。
_DEFAULT_KV: Optional[CloudflareKV] = None


def get_kv(transport=None) -> CloudflareKV:
    """返回进程内共享的 KV 客户端（按环境变量构造）。transport 仅用于测试。"""
    global _DEFAULT_KV
    if transport is not None:
        return CloudflareKV.from_env(transport=transport)
    if _DEFAULT_KV is None:
        _DEFAULT_KV = CloudflareKV.from_env()
    return _DEFAULT_KV
