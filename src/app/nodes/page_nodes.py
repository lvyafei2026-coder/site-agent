# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""建页子图的四个节点（一文件装下：步骤少且严格串行，拆四个文件反而难读）。

    page_plan   取词 + 归类站点 + 解析方案正文 → 页面结构（纯业务，不走 registry）
    page_render render_files：渲染 HTML + sitemap + git add（工具步骤 render）
    page_push   git_push：推送触发构建（工具步骤 push；成功后自动落 agent_pages）
    page_verify check_live_page：线上验活（工具步骤 verify）

统一约定：任何节点发现 state.error 非空就直接放行（错误已定位，不再重复动作）；
本节点失败把 error + stage 写回 state，由条件边短路收尾。
"""

from __future__ import annotations

from typing import Any

from src.app.enums import ClassifyBy
from src.app.services import page_build_service
from src.app.services.tool_service import tool_service


def _pass_if_failed(state: dict[str, Any]) -> dict[str, Any] | None:
    """前置已失败 → 本节点什么都不做（错误信息原样留在 state）。"""
    return {} if state.get("error") else None


def page_plan(state: dict[str, Any]) -> dict[str, Any]:
    """取一条带分析正文的词 → 定目标站点 → 解析出页面结构。

    站点优先用排产计划给的那个（pick_word 从 planned 台账行带出来），没有计划才
    当场 classify_site 判——排产已经判过站点，这里再判一次只会打架。
    三处短路：池里没有可建词 / 归不进任何已有站（建新站流程未实现）/ 解析不出节
    （仍会给出兜底单节，不短路）。confirm=False 时只出预览，由条件边收尾。
    """
    out: dict[str, Any] = {"stage": "plan"}
    picked = page_build_service.pick_word(state.get("term") or None)
    if not picked:
        return {**out, "error": "待开发池里没有带分析正文的词（先在 /agent 跑「候选池分析」）"}
    page = page_build_service.parse_page(picked["term"], picked["text"])
    planned_site = str(picked.get("site") or "")
    cls = ({"site": planned_site, "by": ClassifyBy.PLAN.value} if planned_site
           else page_build_service.classify_site(picked["term"], picked["text"]))
    if not cls["site"]:
        return {**out, "picked": picked, "ai_text": picked["text"], "page": page,
                "site": "", "classify_by": cls["by"],
                "error": (f"「{picked['term']}」归不进任何已有子站，"
                          "需要走「建新站」流程（launch_new_site 工具，人工触发）")}
    return {**out, "picked": picked, "ai_text": picked["text"], "page": page,
            "site": cls["site"], "classify_by": cls["by"]}


def page_render(state: dict[str, Any]) -> dict[str, Any]:
    skip = _pass_if_failed(state)
    if skip is not None:
        return skip
    res = tool_service.execute_for_page("render", site_name=state["site"], page=state["page"])
    if res.get("ok") is False:
        return {"stage": "render", "error": f"渲染失败：{res.get('error', res)}"}
    result = res.get("result") or {}
    return {"stage": "render", "work": str(result.get("work") or "")}


def page_push(state: dict[str, Any]) -> dict[str, Any]:
    skip = _pass_if_failed(state)
    if skip is not None:
        return skip
    res = tool_service.execute_for_page("push", site_name=state["site"],
                                        page=state["page"], work=state.get("work") or "")
    if res.get("ok") is False:
        return {"stage": "push", "error": f"推送失败：{res.get('error', res)}"}
    result = res.get("result") or {}
    # git_push 成功后台账已自动 UPSERT（publishing_tools.git_push），这里只记 sha。
    return {"stage": "push", "sha": str(result.get("sha") or "")}


def page_verify(state: dict[str, Any]) -> dict[str, Any]:
    skip = _pass_if_failed(state)
    if skip is not None:
        return skip
    slug = (state.get("page") or {}).get("slug", "")
    res = tool_service.execute_for_page("verify", site_name=state["site"], slug=slug)
    result = res.get("result") or {}
    ok = bool(res.get("ok")) and result.get("ok") is not False
    return {"stage": "verify", "live_ok": ok, "url": str(result.get("url") or "")}
