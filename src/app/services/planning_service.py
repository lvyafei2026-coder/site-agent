# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""热词创页排产（word_plan_page 专属的共用能力）：待开发词 → 本轮排产方案。

由 processing_service 在取数成功后调用；三份输入各管一个决策维度：
    get_hotwords(todo)    做什么——待开发池
    get_page_inventory    什么已经做了——近 30 天新页（查重 / 防撞车）
    get_reflections       上次怎么翻车的——历史复盘（避坑）
返回 markdown 排产方案正文（选词 + 落站 + 排除原因）。无 key 或调用失败返回
空串，上层跳过该段，不阻塞整轮。提示词来自 D1 的 word_plan_page task。

★入选词必须落台账，否则排产只是会诊：建页线 pick_word 不读这份报告就永远不知道
  该建哪个词。落库形态 = agent_pages 的 planned 行（按优先级顺序插入，rowid 即序），
  建页成功后 git_push 按 (site,slug) 覆盖成 deployed，自动出队。
★报告由代码渲染（render_report），模型只给结构化 items / excluded：报告与落库
  必须是同一批数据，让模型分两处各写一遍迟早对不上。
"""

from __future__ import annotations

import json
import time

from src.app.config import SITES
from src.app.enums import PageStatus, PromptTask
from src.app.llm.completion import complete_json, complete_text
from src.app.observability.langsmith import traceable
from src.app.services.page_build_service import slugify
from src.app.services.prompt_service import prompt_service
from src.app.tools.ledger_tools import LedgerOps
from src.app.utils.json_text import parse_json
from src.app.utils.logger import get_logger
from src.app.utils.text import clip_with_count

MAX_PAYLOAD_CHARS = 8000

log = get_logger(__name__)

# 台账写入用 ledger 域的 ops（与 git_push 落 agent_pages 同一张契约表），
# 不经过 registry：排产落库是流程自动化的产物，不由 LLM 决定写不写。
_ledger = LedgerOps()


def _sites_line() -> str:
    """站群清单（name + brand）随排产 payload 一起给 LLM。

    落站匹配不能只依赖 get_page_inventory 的已建页风格——新站群冷启动时那张表
    是空的，没有这份清单 LLM 只能给「待定」。brand / repo 名自带语义
    （ai-background-remover 之类），足以按词义直接落站，不捏造。
    """
    return [{"name": s.name, "brand": s.brand} for s in SITES.values()]


def _compact_payload(results: dict) -> str:
    """把三个工具的结果压成 LLM 易读的精简 JSON：只取 ok 工具的 result，过长截断。

    ★sites 放最前：payload 超 MAX_PAYLOAD_CHARS 时从尾部截断，工具结果（尤其
    get_hotwords 的 20 条）排在后面被截没关系，站点清单被截掉 LLM 就只能给「待定」。
    """
    slim: dict = {"sites": _sites_line()}
    for name, out in (results or {}).items():
        if isinstance(out, dict) and out.get("ok"):
            slim[name] = out.get("result")
    try:
        text = json.dumps(slim, ensure_ascii=False, default=str)
    except TypeError:
        text = str(slim)
    return clip_with_count(text, MAX_PAYLOAD_CHARS)


def valid_items(raw: object) -> list[dict]:
    """只留能落台账的入选词。

    term 必须非空、site 必须在站群清单里——LLM 会编出不存在的站名，落进去
    会让建页线拿到一个 render 时必然失败的目标站，不如在这里丢掉。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        site = str(item.get("site") or "").strip()
        if not term or site not in SITES:
            continue
        out.append({"term": term, "site": site,
                    "priority": str(item.get("priority") or "").strip(),
                    "reason": str(item.get("reason") or "").strip()})
    return out


def valid_excluded(raw: object) -> list[dict]:
    """本轮排除项：词 + 原因。站名字段不参与，缺 term 的条目丢掉。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        out.append({"term": term, "reason": str(item.get("reason") or "").strip()})
    return out


def render_report(items: list[dict], excluded: list[dict]) -> str:
    """把入选/排除渲染成固定 markdown。

    ★报告由代码渲染而不是要模型写：报告与落台账的必须是同一批数据，模型分两处
    各写一遍迟早对不上（报告说 A、落库是 B）。模型只需给结构化的两个数组。
    """
    if not items:
        return "## 本轮排产\n\n本轮无可排产词。"
    lines = ["## 本轮排产（按优先级）", "",
             "| 优先级 | 词 | 目标站点 | 理由 |", "|---|---|---|---|"]
    lines += [f"| {it['priority'] or '—'} | {it['term']} | {it['site']} | "
              f"{it['reason'] or '—'} |" for it in items]
    if excluded:
        lines += ["", "### 本轮排除"]
        lines += [f"- **{ex['term']}** — {ex['reason'] or '未给原因'}" for ex in excluded]
    return "\n".join(lines)


def _pick_list(data: dict, key: str) -> object:
    """从模型返回里取数组，容忍套一层外壳。

    模型偶尔把结果包成 {"plan": {"items": [...]}} 或 {"result": {...}}——
    只认顶层会让整轮排产静默落空，所以再往下看一层。
    """
    if isinstance(data.get(key), list):
        return data[key]
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return value[key]
    return []


def unwrap(data: dict) -> dict:
    """拆掉 {"type": ..., "content": "<json 或文本>"} 这层外壳。

    实测 json_object 模式下模型会把整份结果塞进 content 字符串里再套一层；
    只拆一层，拆不出可读结构就原样返回（上层按字段名如实报告）。
    """
    if "items" in data or "excluded" in data:
        return data
    inner = data.get("content")
    if not isinstance(inner, str):
        return data
    parsed = parse_json(inner)
    return parsed if isinstance(parsed, dict) else data


class PlanningService:
    @traceable(name="plan_pages")
    def plan(self, results: dict, versions: dict[str, str] | None = None) -> str:
        versions = versions or {}
        system = prompt_service.load_prompt(
            PromptTask.WORD_PLAN_PAGE.value,
            version=versions.get(PromptTask.WORD_PLAN_PAGE.value))
        payload = _compact_payload(results)
        # ★必须走 complete_json：这份提示词的产出要被程序解析后写台账，纯文本补全
        #   下模型会自由发挥成 markdown，落库就断了。拿不到 JSON 才退回纯文本问一次，
        #   至少把报告给人看（这一轮不写库，建页线回退池顺序）。
        data, _failed, _retryable = complete_json(
            system, payload, warn_event="plan_pages_failed")
        if data is None:
            return complete_text(system, payload, warn_event="plan_pages_failed") or ""
        data = unwrap(data)
        items = valid_items(_pick_list(data, "items"))
        excluded = valid_excluded(_pick_list(data, "excluded"))
        if not items and not excluded:
            # 拿到 JSON 但没有可用条目：把顶层字段名写进报告，否则「无可排产词」看不出
            # 到底是池子空了还是模型没按约定出结构（两者的处理方式完全不同）。
            log.warning("排产输出缺 items/excluded，本轮不落库：%s", sorted(data)[:10])
            return ("## 本轮排产\n\n本轮无可排产词。\n\n"
                    f"> 模型返回字段：{', '.join(sorted(data)[:8]) or '空'}"
                    "（未取到 items/excluded）。")
        report = render_report(items, excluded)
        if not items:
            return report
        saved = self.save_plan(items)
        # 落库结果写进报告：排产的产出是「待建页清单」，写没写进去必须让人一眼看见，
        # 否则下一步建页线取不到词时无从判断是没排产还是没落库。
        return (f"{report}\n\n> 已写入台账 {saved}/{len(items)} 条 planned 行"
                f"（建页线按此顺序取词，建页成功后自动转 deployed）。")

    def save_plan(self, items: list[dict]) -> int:
        """入选词按给定顺序写成 planned 台账行，返回实际落库条数。

        (site, slug) 是 agent_pages 的唯一键：同一词同站重复排产只覆盖不重复，
        建页成功后 git_push 的 UPSERT 会把这行改成 deployed，pick_word 自然跳过。
        写入失败只记日志——排产报告照出，不因为台账不可写丢掉整轮结论。
        """
        now = int(time.time())
        saved = 0
        for item in items:
            row = {"site": item["site"], "keyword": item["term"],
                   "slug": slugify(item["term"]),
                   "status": PageStatus.PLANNED.value, "created_at": now}
            try:
                _ledger.ledger_upsert("agent_pages", row)
            except Exception as exc:  # noqa: BLE001 - 台账不可写不能打断排产
                log.warning("计划行写入失败 %s：%s", item["term"], str(exc)[:200])
                continue
            saved += 1
        return saved


planning_service = PlanningService()
