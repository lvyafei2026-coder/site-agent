# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用；详见 LICENSE.

"""推广排期（plan_promo 专属）：已上线页 × 渠道 × 日期 → 可落库的周排期。

背景（2026-11-25）：plan_promo 原先只挂三个只读工具取数（上版排期 / 近 30 天新页 /
待消费事件），取完就没有下文——不出排期、不落库，console 入口只能标「待完善」。
本服务补上后半段，确定性规则（不烧 LLM、不依赖 D1 提示词，发版即生效）：

    build_slots(results)  近 30 天已上线页 × 上版排期 → 去重后的排期条目
    render_report(...)    报告由代码渲染，与落库是同一批数据（模型分两处写迟早对不上）
    plan(results)         生成 + 逐条落 agent_reports(kind='plan') + 回执

★去重三层：①上版排期已推过的 (site, slug) 不再排；②本批同一页只排一次；
③write_promo_slot 自身按 (date, channel) 查重，重复点「排期」不会堆叠同一格。
日期从北京时间明天起顺延；上版已有更晚的排期时从最后一天之后接着排，不挤压旧档。
渠道用 BacklinkChannel 枚举，devto 可经 find_backlinks 自动起稿发布，reddit 需人工跟进。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.app.config import SITES
from src.app.enums import BacklinkChannel, EventKind
from src.app.tools.registry import tool_registry
from src.app.utils.logger import get_logger

log = get_logger(__name__)

# 渠道轮转表（生产可执行的排期渠道，不用 PH/HN——两者早已定档/结案，不走自动排期）。
PROMO_CHANNELS = (BacklinkChannel.DEVTO.value, BacklinkChannel.REDDIT.value)
MAX_SLOTS = 7                    # 一周一档，与 agent_reports 的「下周排期」口径一致
CN_TZ = timezone(timedelta(hours=8))


def _result_of(results: dict, tool: str) -> list[dict]:
    """取一个工具的 result；非 ok 或结构不对都当空，不让坏数据进排期。"""
    out = (results or {}).get(tool) or {}
    if not isinstance(out, dict) or out.get("ok") is False:
        return []
    value = out.get("result")
    return value if isinstance(value, list) else []


def _page_key(item: dict) -> tuple[str, str]:
    return (str(item.get("site") or "").strip(), str(item.get("slug") or "").strip())


def _url_of(site: str, slug: str) -> str:
    """推广目标 = 规范 URL（不带 .html，与 renderer 的 canonical 一致）。"""
    cfg = SITES.get(site)
    return f"{cfg.url}/{slug}" if cfg else ""


def _promoted_keys(latest: list[dict]) -> set[tuple[str, str]]:
    """上版排期里已经推过的页：按 (site, slug) 记，其次用 target_url 兜底。"""
    keys: set[tuple[str, str]] = set()
    for item in latest:
        if not isinstance(item, dict):
            continue
        key = _page_key(item)
        if all(key):
            keys.add(key)
        url = str(item.get("target_url") or "").strip().rstrip("/")
        if url:
            keys.add((url, "*"))
    return keys


def _start_date(latest: list[dict]) -> date:
    """明天起排；上版已有更晚档期就从最后一天之后接，不覆盖旧档。"""
    start = datetime.now(CN_TZ).date() + timedelta(days=1)
    for item in latest:
        if not isinstance(item, dict):
            continue
        try:
            day = date.fromisoformat(str(item.get("date") or "")[:10])
        except ValueError:
            continue
        if day >= start:
            start = day + timedelta(days=1)
    return start


def build_slots(results: dict) -> tuple[list[dict], list[dict]]:
    """(待排条目, 排除项)。纯计算、无副作用——测试可直接验，不碰 D1/KV。"""
    pages = _result_of(results, "get_page_inventory")
    latest = _result_of(results, "get_latest_plan")
    promoted = _promoted_keys(latest)
    start = _start_date(latest)

    slots: list[dict] = []
    excluded: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        if not isinstance(page, dict):
            continue
        site, slug = _page_key(page)
        if not site or not slug:
            continue
        url = _url_of(site, slug)
        if not url:                       # 站点不在清单里，排了也没法发布
            excluded.append({"page": f"{site}/{slug}", "reason": "站点不在群清单"})
            continue
        key = (site, slug)
        if key in promoted or (url, "*") in promoted:
            excluded.append({"page": f"{site}/{slug}", "reason": "上版排期已推广"})
            continue
        if key in seen:
            excluded.append({"page": f"{site}/{slug}", "reason": "本批重复"})
            continue
        seen.add(key)
        index = len(slots)
        slots.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "channel": PROMO_CHANNELS[(len(latest) + index) % len(PROMO_CHANNELS)],
            "site": site,
            "slug": slug,
            "title": str(page.get("keyword") or slug).strip(),
            "target_url": url,
            "reason": "近 30 天上线，尚未推广",
        })
        if len(slots) >= MAX_SLOTS:
            break
    return slots, excluded


def render_report(slots: list[dict], excluded: list[dict], written: int,
                  events: int) -> str:
    """报告由代码渲染：展示的排期就是真正落库的那批条目。"""
    if not slots:
        return "## 本轮推广排期\n\n没有可排期的已上线页（近 30 天新页为空或已全部推广）。"
    lines = ["## 本轮推广排期", "",
             "| 日期 | 渠道 | 页面 | 目标链接 | 理由 |", "|---|---|---|---|---|"]
    for item in slots:
        lines.append(f"| {item['date']} | {item['channel']} | "
                     f"{item['site']}/{item['slug']} | {item['target_url']} | "
                     f"{item['reason'] or '—'} |")
    if excluded:
        lines += ["", "未排：", ""]
        lines += [f"- {e['page']} — {e['reason']}" for e in excluded]
    lines += ["", f"已写入台账 {written}/{len(slots)} 条。"
              f"devto 可经「找外链」自动起稿发布；reddit 需人工跟进。"]
    if events:
        lines.append(f"另有 {events} 条待消费事件（供人工跟进，不参与排期去重）。")
    return "\n".join(lines)


class PromotionPlanningService:
    """取数结果 → 排期条目 → 落库 → 代码渲染报告；落库失败只记回执，不抛异常。"""

    def plan(self, results: dict) -> str:
        slots, excluded = build_slots(results)
        events = len(_result_of(results, "get_pending_events"))
        written = 0
        for item in slots:
            try:
                out = tool_registry.run("write_promo_slot", item=item)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它档期
                log.warning("promo_slot_write_failed", extra={"error": str(exc)[:160]})
                continue
            if isinstance(out, dict) and out.get("ok") is True:
                status = ((out.get("result") or {}).get("status")
                          if isinstance(out.get("result"), dict) else "")
                if status != "skipped":
                    written += 1
        return render_report(slots, excluded, written, events)


promotion_planning_service = PromotionPlanningService()
