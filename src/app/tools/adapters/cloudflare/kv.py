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
REST API 读写，不调任何 App 接口；写侧口径在 hotwords/wordlib.py 复刻 store.js。
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
# ★进程内锁只挡本进程两条线程（后台定时线 + uvicorn 用户线）。Cloudflare KV 无 CAS，
#   跨进程（Render 多实例 / console Workers 人工写）仍可能整表 lost update。
#   为此 atomic_update 额外做「写后读回校验 + 有界重试」：写完 GET 回来，若发现自己的
#   改动被别的写者覆盖，就基于读回的最新值重新跑 mutator 合入再写。这是乐观并发，
#   不是分布式事务——极端同时写仍可能拼输，但批量分析漏写一批词的窗口被堵住了。
_TABLE_LOCK = threading.RLock()

# 读回校验时忽略的易变字段：mutator（upsert_* 等）每次重放都会刷新这些时间戳，
# 若拿它们做相等比较会永远判「被覆盖」→ 无限重试。业务内容按其余字段判定。
_VOLATILE_FIELDS = frozenset(("updatedAt", "at", "ts"))

# 写后读回不一致时的最大重试次数（不含首次写）。0 = 只写一次不校验。
_ATOMIC_MAX_RETRIES = 3

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


def _business_equal(a: Any, b: Any) -> bool:
    """比较两次整表值是否「业务相等」：递归忽略 mutator 每次都会刷新的时间戳字段。

    upsert_* 每次重放都会写新的 updatedAt/at/ts，但业务字段（term / status / reason /
    source / 标签等）不变。写后读回校验只看业务字段，才能区分「我的改动在」和
    「被别的写者整表覆盖」。列表按原顺序逐项比（mutator 都按 at 排序，重放顺序稳定）。
    """
    if isinstance(a, dict) and isinstance(b, dict):
        ka = {k: v for k, v in a.items() if k not in _VOLATILE_FIELDS}
        kb = {k: v for k, v in b.items() if k not in _VOLATILE_FIELDS}
        return ka.keys() == kb.keys() and all(_business_equal(ka[k], kb[k]) for k in ka)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_business_equal(x, y) for x, y in zip(a, b))
    return a == b


def _change_landed(verify: Any, current: Any, updated: Any) -> bool:
    """读回的表里是否已经有我的改动——按「本次真正改动的项」逐项确认，不整表比相等。

    Cloudflare KV 是全球最终一致的：写成功（PUT 200）后立刻读回，可能拿到旧值
    （我的行还没出现），也可能拿到别的写者在我之后又追加了行的版本。这两种情况
    用「读回值 == 本次写入值」判断都会误判成「被覆盖」。

    做法：以写入前的 current 为基准，找出 updated 里业务上真正变了的项（行 / 键），
    逐项看读回值 verify 是否已是写入后的内容：
    - 读回含我的改动（哪怕还多了别人的行）→ True，落住了。
    - 读回是旧值（我的项还是旧内容）→ False，需重试。
    - 读回是被覆盖的版本（我的项没了）→ False，需重试合入。
    ★不重跑 mutator：调用方的 mutator 可能带副作用（如 state_sync 用闭包带出
      changes），重跑会把明细清空；这里只读三份快照，纯比较。
    """
    if isinstance(updated, dict) and isinstance(current, dict) and isinstance(verify, dict):
        for k, v in updated.items():
            if _business_equal(current.get(k), v):
                continue
            if not _business_equal(verify.get(k), v):
                return False
        # 本次删掉的键：读回里必须也已消失
        for k in current:
            if k not in updated and k in verify:
                return False
        return True
    if isinstance(updated, list) and isinstance(current, list) and isinstance(verify, list):
        def rows(xs: list) -> dict:
            return {x.get("key"): x for x in xs if isinstance(x, dict) and x.get("key")}
        cur, ver = rows(current), rows(verify)
        for row in updated:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            k = row["key"]
            if _business_equal(cur.get(k), row):
                continue
            if not _business_equal(ver.get(k), row):
                return False
        upd_keys = {x.get("key") for x in updated if isinstance(x, dict)}
        for k in cur:
            if k not in upd_keys and k in ver:
                return False
        return True
    return _business_equal(verify, updated)


def atomic_update(
    kv: Any,
    key: str,
    mutator: Callable[[Any], Any],
    *,
    reader: Optional[Callable[[Any, str], Any]] = None,
    ttl: Optional[int] = None,
) -> tuple[bool, Any]:
    """整表的「读 → 改 → 写 → 写后读回确认」乐观并发更新。

    进程内用 _TABLE_LOCK 串行；写后再 GET 回来，用 `_change_landed`（幂等判定）
    确认我的改动是否已在表里：
      - 已落住 → 成功返回 (True, updated)。
      - 没落住（读回是旧值 / 被别的进程整表覆盖）→ 以读回值为 current 重跑 mutator
        （幂等 upsert，重放安全）再写，最多重试 _ATOMIC_MAX_RETRIES 次。

    ★返回 ok=False 只表示「PUT 真的没写进去」（网络错 / 凭据错 / CF 5xx），调用方
    必须据此把该词记 error。写已成功但读回拿不到确认（KV 最终一致性、读回异常、
    重试耗尽）一律返回 True + warn：数据已经落住，判失败只会造成「线上有数据、
    页面报失败」的误报，还会诱导重复分析、重复写整表。

    Cloudflare KV 无 CAS，这是乐观并发而非分布式事务：重试每轮都基于读回的最新值
    合入对方的行，堵住「批量分析写 state 表时被人工操作整表覆盖、漏掉一批词」的窗口。
    `reader` 注入形状兜底（list 用 kv_list、dict 用 kv_dict），防止形状漂移把脏数据
    当空表写回。`mutator` 应是纯函数：不要在里面做网络请求，否则持锁期间一次外部
    超时会卡住整批。
    """
    read = reader if reader is not None else (lambda k, kk: k.get_json(kk, {}) or {})
    last_updated: Any = None
    for attempt in range(_ATOMIC_MAX_RETRIES + 1):
        with _TABLE_LOCK:
            try:
                current = read(kv, key)
                updated = mutator(current)
                last_updated = updated
                ok = bool(kv.put_json(key, updated, ttl=ttl))
            except Exception as exc:  # noqa: BLE001 - 任何一步失败都要变成「写失败」而不是中断整批
                log.warning("KV 整表更新异常 key=%s: %s", key, exc)
                return False, None
            if not ok:
                log.warning("KV 整表写入未成功 key=%s（put_json 返回 False）", key)
                return False, None
            try:
                verify = read(kv, key)
                landed = _change_landed(verify, current, updated)
            except Exception as exc:  # noqa: BLE001
                # 写已经成功，读回/判定异常只是「没确认」，绝不能当成写失败
                log.warning("KV 写后读回确认失败 key=%s（写入已成功）: %s", key, exc)
                return True, updated
        if landed:
            return True, updated
        log.warning("KV 写后读回未确认 key=%s（可能是旧值或被覆盖），重试 %d/%d",
                    key, attempt + 1, _ATOMIC_MAX_RETRIES)

    # 重试耗尽：每一次 PUT 都成功，且每轮都基于读回的最新值重跑 mutator 合入了对方的行，
    # 数据不会丢；只是 KV 最终一致性让读回一直没确认到自己的改动。按成功返回并留痕。
    log.warning("KV 写后读回连续未确认 key=%s，重试 %d 次耗尽；写入均已成功，按成功处理",
                key, _ATOMIC_MAX_RETRIES)
    return True, last_updated


# 模块级单例：生产路径从这里取，避免每批词重建客户端。
_DEFAULT_KV: Optional[CloudflareKV] = None


def get_kv() -> CloudflareKV:
    """返回进程内共享的 KV 客户端（按环境变量构造）。"""
    global _DEFAULT_KV
    if _DEFAULT_KV is None:
        _DEFAULT_KV = CloudflareKV.from_env()
    return _DEFAULT_KV
