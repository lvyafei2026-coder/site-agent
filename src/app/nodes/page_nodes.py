# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""建页子图的六个节点（一文件装下：步骤少且严格串行，拆六个文件反而难读）。

    page_plan   取词 + 归类站点 + 解析方案正文 → 页面结构（纯业务，不走 registry）
    page_audit  通读原站：判重 + 定增强/新增 + 抽风格基调（纯业务，读已 clone 仓库）
    page_write  分析稿 → 访客可读正文；仍是分析稿就在渲染前拦下
    page_render render_files：渲染 HTML + sitemap + git add（工具步骤 render）
    page_push   git_push：推送触发构建（工具步骤 push；成功后自动落 agent_pages）
    page_verify check_live_page：线上验活（工具步骤 verify）

统一约定：任何节点发现 state.error 非空就直接放行（错误已定位，不再重复动作）；
本节点失败把 error + stage 写回 state，由条件边短路收尾。
"""

from __future__ import annotations

from typing import Any

from src.app.enums import AuditDecision, ClassifyBy, ContentSource, PageBuildStep
from src.app.services import page_build_service, term_state_service
from src.app.services.page_content_service import analysis_leak, page_content_service
from src.app.services.site_audit_service import audit as audit_site
from src.app.services.site_audit_service import read_site, style_brief
from src.app.services.tool_service import tool_service


def _pass_if_failed(state: dict[str, Any]) -> dict[str, Any] | None:
    """前置已失败 → 本节点什么都不做（错误信息原样留在 state）。"""
    return {} if state.get("error") else None


def page_plan(state: dict[str, Any]) -> dict[str, Any]:
    """取一条带分析正文的词 → 定目标站点 → 解析出页面结构。

    站点优先用排产计划给的那个（pick_word 从 planned 台账行带出来，或调用方随
    词一起点名的 planned_site），没有计划才当场 classify_site 判——排产已经判过
    站点，这里再判一次只会打架。
    三处短路：池里没有可建词 / 归不进任何已有站（建新站流程未实现）/ 解析不出节
    （仍会给出兜底单节，不短路）。confirm=False 时只出预览，由条件边收尾。
    """
    out: dict[str, Any] = {"stage": "plan"}
    picked = page_build_service.pick_word(state.get("term") or None)
    if not picked:
        return {**out, "error": "待开发池里没有带分析正文的词（先在 /agent 跑「候选池分析」）"}
    page = page_build_service.parse_page(picked["term"], picked["text"])
    # 站点来源优先级：排产台账给的那一行（pick_word 带出）→ 调用方点名（从台账点单时
    # 只传词和站）→ 现场 classify。前两者都是「排产已判过」，不能再判一次。
    planned_site = page_build_service.valid_site(
        str(picked.get("site") or state.get("planned_site") or ""))
    cls = ({"site": planned_site, "by": ClassifyBy.PLAN.value} if planned_site
           else page_build_service.classify_site(picked["term"], picked["text"]))
    if not cls["site"]:
        return {**out, "picked": picked, "ai_text": picked["text"], "page": page,
                "site": "", "classify_by": cls["by"],
                "error": (f"「{picked['term']}」归不进任何已有子站，"
                          "需要走「建新站」流程（launch_new_site 工具，人工触发）")}
    return {**out, "picked": picked, "ai_text": picked["text"], "page": page,
            "site": cls["site"], "classify_by": cls["by"]}


def page_audit(state: dict[str, Any]) -> dict[str, Any]:
    """通读原站：先看这站已经有什么，再决定新增 / 增强 / 拦下。

    ★读的是目标站仓库里的 HTML（与渲染阶段同一份快照），不是线上抓——
    这样预览与上线看到的是同一份"原站现状"。clone / 读失败都降级为「没读到」，
    不阻塞建页，但 reason 会进结果面板。
    """
    skip = _pass_if_failed(state)
    if skip is not None:
        return skip
    site_name = str(state.get("site") or "")
    term = str((state.get("picked") or {}).get("term") or state.get("term") or "")
    digest = read_site(site_name)
    verdict = audit_site(term, digest)
    out: dict[str, Any] = {
        "stage": PageBuildStep.AUDIT.value,
        "site_digest": digest,
        "site_audit": verdict,
    }
    if verdict["decision"] == AuditDecision.SKIP.value:
        # 显式增强模式：人工已经看过重复证据、确认要在原页上做增量，才放行覆盖。
        if state.get("mode") == "enhance":
            verdict = {**verdict, "decision": AuditDecision.ENHANCE.value,
                       "reason": f"显式增强模式：{verdict['reason']}；本次在已有页上做增量"}
            return {**out, "site_audit": verdict}
        return {**out, "error": f"站情校验拦下：{verdict['reason']}"}
    return out


def page_write(state: dict[str, Any]) -> dict[str, Any]:
    """把分析稿改写成访客可读正文；写不成仍保留解析稿供预览，但含分析用语就拦下。

    ★为什么拦在渲染前：这种内容一旦推上线，就是「把内部决策稿给访客看」；而且推送
      会覆盖同 slug 的历史文件，事后才发现只能再推一次。宁可这一轮明确失败。
    """
    skip = _pass_if_failed(state)
    if skip is not None:
        return skip
    term = str((state.get("picked") or {}).get("term") or "")
    draft = dict(state.get("page") or {})
    # 把原站"已有什么 + 风格基调 + 判重结论"塞进写作 payload（D1 提示词正文不改）
    brief = style_brief(state.get("site_digest") or {}, state.get("site_audit") or {})
    written = page_content_service.write(
        term, state["site"], state.get("ai_text") or "", site_brief=brief)
    page = {**draft, **written} if written else draft
    out: dict[str, Any] = {
        "stage": PageBuildStep.WRITE.value,
        "page": page,
        "content_by": ContentSource.LLM.value if written else ContentSource.DRAFT.value,
    }
    leak = analysis_leak(page)
    if leak:
        return {**out, "error": (
            f"页面正文没写成：解析稿里仍带内部分析用语「{leak}」，已在渲染前拦下。"
            "去 /rules/prompts 确认 page_content 提示词可读、LLM 额度正常，再重试本轮。")}
    return out


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
    # ★闭环：该词已上线 → saved 表同步为 live，从「待开发/开发中」出队。
    term = str((state.get("picked") or {}).get("term") or state.get("term") or "")
    if term:
        term_state_service.after_push(term)
    return {"stage": "push", "sha": str(result.get("sha") or "")}


def page_verify(state: dict[str, Any]) -> dict[str, Any]:
    skip = _pass_if_failed(state)
    if skip is not None:
        return skip
    slug = (state.get("page") or {}).get("slug", "")
    # 带上 sha：verify 侧先等该 sha 的 CF 构建跑完再打线上，否则刚推完必然 404
    res = tool_service.execute_for_page("verify", site_name=state["site"], slug=slug,
                                       sha=state.get("sha") or "")
    result = res.get("result") or {}
    ok = bool(res.get("ok")) and result.get("ok") is not False
    return {"stage": "verify", "live_ok": ok, "url": str(result.get("url") or "")}
