# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""★KV 操作的唯一模块：连接、读写、列举、原子更新、锁、两种语义门面全在这里。

指向**同一个 KV 命名空间**（KV_NAMESPACE_ID）的两个门面，差别只在键前缀与错误语义：
    CloudflareKV  共享业务表（RADAR_KV：saved / rejected / ai:v3: / serp:v1: 等，
                  键名与 console/src/store.js、radar/src/store.js 严格对齐，见下方常量）。
                  fail-open：读失败返回默认值并留痕，绝不因 KV 抖动中断一批词。
    WorkersKV     agent 私有状态标记（键统一带 `agent:` 前缀）。
                  fail-fast：出错直接抛异常，状态标记写不进去必须让人知道。
                  建立在同一个 CloudflareKV 底层请求之上。

架构约束（2026-11-12 定）：radar / console / agent 共用这个 KV 命名空间，agent 直接经
REST API 读写，不调任何 App 接口；写侧口径在 terms/curation.py 复刻 store.js。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
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

# agent 会并发写回的 5 张「整表」：每张都是「读全表 → 改一项 → 写全表」。
# candidates / tags 只读，不在此列；ai:v3: / serp:v1: / trend:v1: 是单词独立键，天然不冲突。
SHARED_TABLE_KEYS: tuple[str, ...] = (
    SAVED_KEY, REJECTED_KEY, HINTS_KEY, COMPETITION_KEY, INTERESTS_KEY,
)

# ★一把进程级 RLock 覆盖 5 张整表（不是每表一把）：跨表组合写入也要串行，
#   否则「先写 saved、再写 aihints」之间仍可能被另一个 worker 插进来读旧值。
#   用 RLock 而非 Lock：mutator 里若再调一次 atomic_update（回填等场景）不会自锁死。
# ★边界：这把锁只保证**本进程内** 4 个 batch worker 不互相覆盖。Cloudflare KV 无 CAS、
#   最终一致，跨进程（Render 多实例）/ console 人工同时写仍可能 lost update；
#   后者靠「写失败记 error + backfill_states 状态回填」兜底，别把锁当成分布式事务。
_TABLE_LOCK = threading.RLock()

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
    """Cloudflare KV REST 客户端（fail-open）：读写共享业务表，失败降级不中断。"""

    def __init__(
        self,
        account_id: str,
        namespace_id: str,
        api_token: str,
    ) -> None:
        self.account_id = account_id
        self.namespace_id = namespace_id
        self.api_token = api_token

    @classmethod
    def from_env(cls) -> "CloudflareKV":
        return cls(
            account_id=os.environ.get("CF_ACCOUNT_ID", ""),
            namespace_id=os.environ.get("KV_NAMESPACE_ID", ""),
            api_token=os.environ.get("CF_API_TOKEN", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.namespace_id and self.api_token)

    # ---- 底层请求（两个门面共用这一条路径）----
    def _request(self, method: str, key: Optional[str], *, params=None, body=None) -> tuple[int, str]:
        # ★键名必须百分号编码：我们的键是 "ai:v3:free invoice calculator" 这种含冒号/空格/可能含
        #   # ? % 的自然语言词组，裸拼进路径会被 URL 解析截断或改写（Cloudflare 文档明确要求编码）。
        if key is not None:
            url = _api_base(self.account_id, self.namespace_id) + "/values/" + urllib.parse.quote(key, safe="")
        else:
            url = _api_base(self.account_id, self.namespace_id) + "/keys"

        # __list__ 是内部标记（区分 /keys 与 /values），绝不能发给 Cloudflare
        q = {k: v for k, v in (params or {}).items() if k != "__list__"} or None

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
        if not self.configured:
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


class WorkersKV:
    """agent 私有状态标记的门面：键统一带 `agent:` 前缀，fail-fast。

    与 CloudflareKV（共享业务表、fail-open）的差异见模块 docstring。
    建立在同一 CloudflareKV 的底层请求上——全项目只有一条 KV HTTP 路径。
    出错抛 RuntimeError（而不是吞成默认值）：状态标记写不进去必须让人知道。
    """

    PREFIX = "agent:"

    def __init__(self, kv: CloudflareKV):
        self._kv = kv

    def _name(self, key: str) -> str:
        return key if key.startswith(self.PREFIX) else self.PREFIX + key

    def get(self, key: str, default: Any = None) -> Any:
        status, text = self._kv._request("GET", self._name(key))
        if status == 404:
            return default
        if status != 200:
            raise RuntimeError(f"KV 读取失败 {self._name(key)}: HTTP {status} {str(text)[:120]}")
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    def put(self, key: str, value: Any, ttl_sec: int | None = None) -> None:
        params = {"expiration_ttl": ttl_sec} if ttl_sec else None
        try:
            body = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"KV 写入 {self._name(key)} 序列化失败: {exc}") from exc
        status, text = self._kv._request(
            "PUT", self._name(key), params=params, body=body)
        if status not in (200, 201):
            raise RuntimeError(f"KV 写入失败 {self._name(key)}: HTTP {status} {str(text)[:120]}")


def table_lock() -> threading.RLock:
    """需要「多张表一起改完才放开」的调用方（如状态回填）可以直接拿这把锁。"""
    return _TABLE_LOCK


def atomic_update(
    kv: Any,
    key: str,
    mutator: Callable[[Any], Any],
    *,
    reader: Optional[Callable[[Any, str], Any]] = None,
    ttl: Optional[int] = None,
) -> tuple[bool, Any]:
    """把整表的「读 → 改 → 写 → 校验返回值」包成一次原子操作。

    返回 `(ok, updated)`：
      ok=False 表示读/改/写任一步抛异常，或 put_json 返回了假值（凭据失效 / 5xx /
      序列化失败）。调用方必须据此把该词记为 error，不能默认写入成功。

    `reader` 允许调用方注入形状兜底（期望 list 就是 _kv_list、dict 就是 _kv_dict），
    避免形状漂移时把脏数据当成空表再写回去。`mutator` 应当是纯函数：不要在锁内
    做网络请求，否则整批 worker 会被一次外部超时全部卡住。
    """
    with _TABLE_LOCK:
        try:
            current = reader(kv, key) if reader is not None else (kv.get_json(key, {}) or {})
            updated = mutator(current)
            ok = bool(kv.put_json(key, updated, ttl=ttl))
        except Exception as exc:  # noqa: BLE001 - 任何一步失败都要变成「写失败」而不是中断整批
            log.warning("KV 整表更新异常 key=%s: %s", key, exc)
            return False, None
        if not ok:
            log.warning("KV 整表写入未成功 key=%s（put_json 返回 False）", key)
        return ok, updated


# 模块级单例：生产路径从这里取，避免每批词重建客户端。
_DEFAULT_KV: Optional[CloudflareKV] = None


def get_kv() -> CloudflareKV:
    """返回进程内共享的 KV 客户端（按环境变量构造）。"""
    global _DEFAULT_KV
    if _DEFAULT_KV is None:
        _DEFAULT_KV = CloudflareKV.from_env()
    return _DEFAULT_KV
