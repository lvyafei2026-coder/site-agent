# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""SERP 抓取 + 竞争度打分（从 radar/src/serp.js 原样移植到 Python）。

为什么用 DuckDuckGo HTML 版：免 key、免登录、返回真实前 10 名；Google 前端结果在
服务端拿不到（JS 渲染 + 反爬），Bing 结构随地区漂移。

★2026-11-13 两轮实测，两个不同层次的故障别混为一谈：
  ① HTTP 层「202 挑战页」：根因不是无 JS / 反爬升级，而是**我们自己多带的头**。
     只发 User-Agent 时各端点稳定返回 200 + 10 条真实结果；显式带上 Accept /
     Accept-Language / Referer 任意一个就是 202 anomaly 页。requests 自己补的默认头
     （Accept: */*、Accept-Encoding、Connection）是安全的，别手动覆盖。
  ② 网络层「连接超时」：与请求头无关，是**运行环境的出口 IP 到 DDG 不通**。本机直连 /
     走代理、以及另一个云端出口都实测拿到真结果；Render singapore 出口则 4 发全部
     ConnectTimeout。为此 PLAN 覆盖 4 个不同域名（DNS 各给一组不同 IP），并加两层换出口：
     SERP_HTTPS_PROXY（部署侧自备代理，只作用于 SERP），以及直连全挂时自动走公共
     Reader 中转（SERP_FALLBACK_READER=0 可关）。两者都不影响 KV REST / LLM / GitHub。

纯函数（parse_ddg / domain_of / decode_uddg / score_competition / serp_verdict）不碰网络，
单测直接喂 HTML 即可。fetch_serp 是网络层，session 可注入，便于离线测试。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

import requests

from .errors import SourceUnavailable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
# ★只留 User-Agent。2026-11-13 逐头实测：额外显式设置 Accept / Accept-Language /
#  Referer 任意一个都会触发 DDG 的 202 anomaly 挑战页；requests 自动补的默认头无害。
#  别为了「更像浏览器」把头加回来——那正是挑战页的触发条件。
BASE_HEADERS = {"User-Agent": UA}

# 可选：只给 SERP 换出口（Render 等云端出口连不上 DDG 时在部署侧填）。
# 不用 HTTPS_PROXY 是为了不影响同一进程里的 KV REST / LLM / GitHub 等出站调用。
# 留空时 requests 仍按默认行为读进程环境，行为与改动前一致。
SERP_HTTPS_PROXY = os.environ.get("SERP_HTTPS_PROXY", "").strip()


def _ep_html(q: str) -> str:
    return "https://html.duckduckgo.com/html/?q=" + q + "&kl=us-en"


def _ep_main(q: str) -> str:
    return "https://duckduckgo.com/html/?q=" + q + "&kl=us-en"


def _ep_lite(q: str) -> str:
    return "https://lite.duckduckgo.com/lite/?q=" + q + "&kl=us-en"


def _ep_www(q: str) -> str:
    return "https://www.duckduckgo.com/html/?q=" + q + "&kl=us-en"


# ★4 发打 4 个不同 host（DNS 轮询各给一组不同 IP）：某个子域被污染 / 某段出口 IP 被拦时，
#  其余路径仍有机会。2026-11-13 本机实测四条路径都返回结构一致、parse_ddg 可直接消费的
#  10 条真结果。engine 只保留两种旧值，写进 KV 的 serp:v1: 口径不变。
#  host 字段只用于失败诊断（到底哪些域名连不上），不参与抓取。
PLAN = [
    {"engine": "duckduckgo",      "host": "html.duckduckgo.com", "ep": _ep_html, "wait": 0},
    {"engine": "duckduckgo",      "host": "duckduckgo.com",      "ep": _ep_main, "wait": 1200},
    {"engine": "duckduckgo-lite", "host": "lite.duckduckgo.com", "ep": _ep_lite, "wait": 2500},
    {"engine": "duckduckgo",      "host": "www.duckduckgo.com",  "ep": _ep_www,  "wait": 4000},
]

# 直连 4 发全没拿到真结果时的「换出口」兜底：公共 Reader 云端中转，取回的是**原始
# DDG HTML**，serp_verdict / parse_ddg / score_competition 全部复用，口径与直连一致。
# 2026-11-13 实测：x-respond-with: html 返回含 10 个 result__a 的原页，3 个词连测
# 全部 200、0.8~1.4s。它是通用网页阅读服务、不是 SERP 转卖商；默认开启，
# 设 SERP_FALLBACK_READER=0 可完全关掉（回到纯直连）。
READER_BASE = "https://r.jina.ai/"
READER_HEADERS = {"User-Agent": UA, "x-respond-with": "html"}
SERP_FALLBACK_READER = os.environ.get("SERP_FALLBACK_READER", "1").strip().lower() not in (
    "0", "false", "no", "")


def _fetch_via_reader(q: str, limit: int, sess: Any, proxy: Optional[str]) -> dict:
    """经 Reader 取原始 DDG HTML；session 可注入，离线测试完全可控。"""
    kwargs: dict[str, Any] = {"headers": READER_HEADERS, "timeout": (6, 20)}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    resp = sess.get(READER_BASE + _ep_html(q), **kwargs)
    return serp_verdict(resp.status_code, resp.text, limit)


# ★直连被判定不通后，短时间内不再每个词都撞 4 次 6 秒 connect 超时。
#   Render 出口全挂时每词要白等约 32 秒，20 词 ≈ 10 分钟，免费实例会被拖到请求超时。
#   记住状态后同一批后续词直接走兜底出口；DIRECT_RETRY_SEC 到期会再探一次直连。
#   进程内状态，重启即清零；只是性能优化，不影响结果口径与中止语义。
DIRECT_RETRY_SEC = 300
_STATE = {"direct_down_until": 0.0}

RE_BLOCK = re.compile(r"anomaly|unusual traffic|are you a robot|captcha", re.I)
RE_NO_RESULT = re.compile(r"no-results|No results found|没有找到", re.I)

# 纯函数：判定这一发是「真结果 / 真空结果 / 挑战页」，方便单测覆盖
def serp_verdict(status: int, html: str, limit: int = 10):
    body = str(html or "")
    if status != 200:
        kind = "challenge" if status in (202, 429) or status >= 500 else "error"
        return {"ok": False, "kind": kind, "status": status, "items": [],
                "note": "202 挑战页" if status == 202 else "HTTP " + str(status)}
    items = parse_ddg(body, limit)
    if items:
        return {"ok": True, "kind": "items", "status": status, "items": items, "note": ""}
    if RE_BLOCK.search(body) or (len(body) < 20000 and re.search(r"<title>\s*DuckDuckGo\s*</title>", body, re.I)):
        return {"ok": False, "kind": "challenge", "status": status, "items": [], "note": "202 挑战页"}
    if RE_NO_RESULT.search(body):
        return {"ok": True, "kind": "empty", "status": status, "items": [], "note": "无结果"}
    return {"ok": False, "kind": "challenge", "status": status, "items": [], "note": "解析为空"}


_ENT = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'", "#x27": "'", "nbsp": " "}


def _clean_text(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"<[^>]*>", "", s)
    def _ent(m):
        k = m.group(1).lower()
        if k in _ENT:
            return _ENT[k]
        if k.startswith("#x"):
            return chr(int(k[2:], 16) or 32)
        if k.startswith("#"):
            return chr(int(k[1:], 10) or 32)
        return m.group(0)
    s = re.sub(r"&(#x?[0-9a-f]+|[a-z]+);", _ent, s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def decode_uddg(href: str) -> str:
    """DDG 跳转链接 //duckduckgo.com/l/?uddg=<encodeURIComponent(真实地址)>。"""
    href = str(href or "")
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        try:
            import urllib.parse as up
            return up.unquote(m.group(1))
        except Exception:
            return ""
    return href if re.match(r"^https?://", href, re.I) else ""


def domain_of(url: str) -> str:
    try:
        host = re.sub(r"^www\.", "", __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url).hostname or "")
        return host.lower()
    except Exception:
        return ""


def parse_ddg(html: str, limit: int = 10) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"<a([^>]+)>([\s\S]*?)</a>", str(html or "")):
        attrs = m.group(1)
        if not re.search(r"result__a|result-link", attrs):
            continue
        href = re.search(r'href=["\']([^"\']*)["\']', attrs)
        href = href.group(1) if href else ""
        url = decode_uddg(href)
        domain = domain_of(url)
        if not url or not domain:
            continue
        dedupe = domain + url
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append({"url": url, "domain": domain, "title": _clean_text(m.group(2))})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 站点类型词表（只收"一眼能认出来"的，宁可漏判也不乱扣分）
# ---------------------------------------------------------------------------
AUTH = ["wikipedia.org", "britannica.com", "wikiwand.com", "fandom.com", "wikidata.org"]
MEDIA = [
    "nytimes.com", "bbc.com", "bbc.co.uk", "cnn.com", "forbes.com", "reuters.com", "apnews.com",
    "theguardian.com", "washingtonpost.com", "businessinsider.com", "cnbc.com", "usatoday.com",
    "dailymail.co.uk", "thesun.co.uk", "foxnews.com", "nbcnews.com", "abcnews.go.com", "cbsnews.com",
    "npr.org", "newsweek.com", "time.com", "huffpost.com", "independent.co.uk", "telegraph.co.uk",
    "economist.com", "wsj.com", "bloomberg.com", "today.com", "people.com", "variety.com",
    "deadline.com", "hollywoodreporter.com", "elle.com", "vogue.com", "cosmopolitan.com",
    "buzzfeed.com", "screenrant.com", "collider.com", "imdb.com", "tmz.com", "pagesix.com",
    "mirror.co.uk", "express.co.uk", "sky.com", "ndtv.com", "hindustantimes.com", "thehindu.com",
    "news18.com", "timesofindia.indiatimes.com", "thetimes.co.uk", "scmp.com", "straitstimes.com",
]
TOOLS = [
    "calculator.net", "omnicalculator.com", "calculatorsoup.com", "smartasset.com", "nerdwallet.com",
    "bankrate.com", "investopedia.com", "thepointsguy.com", "icalculator.com", "salary.com",
    "glassdoor.com", "indeed.com", "zillow.com", "redfin.com", "kbb.com", "edmunds.com",
    "caranddriver.com", "motortrend.com", "numbeo.com", "xe.com", "oanda.com", "paycheckcity.com",
    "goodcalculators.com", "calculatorpro.com", "thecalculator.site", "finmasters.com",
]
UGC = ["reddit.com", "quora.com", "stackexchange.com", "x.com", "twitter.com", "facebook.com",
       "tiktok.com", "youtube.com", "instagram.com", "pinterest.com", "linkedin.com"]
LOW = ["blogspot.com", "medium.com", "wordpress.com", "substack.com", "wixsite.com", "weebly.com",
       "github.io", "sites.google.com", "newsbreak.com", "msn.com", "linkedin.com",
       "facebook.com", "pinterest.com", "youtube.com"]
ECOM = ["amazon.com", "ebay.com", "etsy.com", "walmart.com", "target.com", "bestbuy.com",
        "aliexpress.com", "temu.com", "shein.com", "flipkart.com", "myntra.com", "nykaa.com"]


def _is_gov(d: str) -> bool:
    return bool(re.search(r"(\.gov|\.edu|\.mil)(\.[a-z]{2})?$", d) or re.search(r"\.(ac|gov|edu)\.[a-z]{2}$", d))


def _in_list(d: str, lst: list[str]) -> bool:
    return any(d == x or d.endswith("." + x) for x in lst)


def _count_in(domains: list[str], lst: list[str]) -> int:
    return sum(1 for d in domains if _in_list(d, lst))


def _term_tokens(term: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(term or "").lower())
            if len(t) >= 4 and t not in {"2024", "2025", "2026", "2027", "price", "cost", "date",
                                         "dates", "near", "best"}]


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


# 0-100，越高越难抢。返回分 + 等级 + 人话理由
def score_competition(items: list[dict], term: str = "") -> dict[str, Any]:
    lst = [x for x in (items or []) if isinstance(x, dict)]
    domains = [x.get("domain") for x in lst if x.get("domain")]
    uniq = list(dict.fromkeys(domains))
    reasons: list[str] = []
    score = 40

    wiki_pos = next((i for i, d in enumerate(domains) if _in_list(d, AUTH)), -1)
    if wiki_pos >= 0:
        score += 26 if wiki_pos == 0 else 18
        reasons.append(("维基百科排第 1" if wiki_pos == 0 else "维基百科在前 10") + "，信息型查询的头部基本锁死")

    gov_n = sum(1 for d in domains if _is_gov(d))
    if gov_n:
        score += 14 + (4 if gov_n >= 2 else 0)
        reasons.append(f"{gov_n} 个政府/学校官方域名占位，权威度压不住")

    media_n = _count_in(domains, MEDIA)
    if media_n >= 4:
        score += 22
        reasons.append(f"{media_n} 个大型媒体占位（{', '.join(uniq[:2])}），内容供给充足")
    elif media_n == 3:
        score += 16
        reasons.append(f"3 个大型媒体占位（{', '.join(uniq[:2])}）")
    elif media_n == 2:
        score += 10
        reasons.append("2 个大型媒体占位")
    elif media_n == 1:
        score += 5

    tool_n = _count_in(domains, TOOLS)
    if tool_n >= 2:
        score += 16
        reasons.append(f"{tool_n} 个成熟工具站已存在（{', '.join(uniq[:2])}），要正面拼功能和权重")
    elif tool_n == 1:
        score += 9
        reasons.append(f"已有工具站：{next((d for d in uniq if _in_list(d, TOOLS)), '')}")

    tokens = _term_tokens(term)
    official = next((d for d in uniq
                    if any(t in d.split(".")[0] for t in tokens)
                    and not _in_list(d, MEDIA + UGC + ECOM + TOOLS) and not _is_gov(d)), None)
    if official:
        score += 16
        reasons.append(f"品牌/官方站 {official} 在前 10，产品词抢不过官网")

    forum = next((d for d in domains[:10] if _in_list(d, ["reddit.com", "quora.com", "stackexchange.com"])), None)
    if forum:
        score -= 6
        reasons.append(f"{forum} 在结果里，说明没有强势工具站，社区在替用户解决问题")
    top3_ugc = any(_in_list(d, ["youtube.com", "tiktok.com", "pinterest.com", "instagram.com"]) for d in domains[:3])
    if top3_ugc:
        score -= 8
        reasons.append("前 3 名有视频/图片平台，说明搜索意图以\"看\"为主，工具页有机会")

    low_n = _count_in(domains, LOW)
    if low_n >= 4:
        score -= 14
        reasons.append(f"前 10 有 {low_n} 个自媒体/低门槛页，大站没认真做")
    elif low_n >= 2:
        score -= 8
        reasons.append(f"{low_n} 个低门槛内容页占位，内容质量普遍不高")

    ecom_n = _count_in(domains, ECOM)
    if ecom_n >= 3:
        score += 6
        reasons.append(f"{ecom_n} 个电商平台占位，购买意图已被大平台吃掉")

    if lst and len(lst) < 6:
        score -= 12
        reasons.append(f"前 10 只返回 {len(lst)} 条，长尾竞争稀薄")

    score = _clamp(round(score), 5, 95)
    level = "高" if score >= 80 else "较高" if score >= 60 else "中" if score >= 35 else "低"
    return {
        "score": score,
        "level": level,
        "reasons": reasons or ["前 10 名没有明显的权威站或成熟工具站，属于可打的空档"],
        "counts": {"wiki": 1 if wiki_pos >= 0 else 0, "media": media_n, "tools": tool_n,
                   "gov": gov_n, "ugc": _count_in(domains, UGC), "low": low_n, "ecom": ecom_n},
        "domains": uniq[:10],
    }


def fetch_serp(term: str, session: Optional[Any] = None, limit: int = 10) -> dict[str, Any]:
    """抓真实 SERP 前 10，4 发退避、4 个不同 host。session 可注入（测试用）。

    返回 {engine, items, attempts}；失败抛 SourceUnavailable（源级不可用，调用方应在
    整批里立刻停手，等下次触发重试），异常里带 attempts 便于诊断。

    ★2026-11-13 修正：11-12 判定的「DDG 已废」是误判，根因是 BASE_HEADERS 里多带的
      Accept / Accept-Language / Referer 触发了 202 anomaly 页。改成只发 User-Agent 后，
      四条路径均稳定 200 + 10 条真实结果，parse_ddg / score_competition 可直接消费。
      Bing（含 &format=rss）仍返回结构合法、内容无关的假结果，不留兜底源。
    ★同日第二轮：Render singapore 出口报「4 发全部连接失败」，这属于网络层、与请求头无关
      （本机直连 / 代理、另一云端出口都能拿到真结果）。PLAN 因此改成 4 个不同域名，
      失败诊断列出全部 host + 异常类型；直连全挂时再自动走 Reader 中转换出口，
      仍失败才抛 SourceUnavailable（用户定的「遇异常退出等下次重试」不变）。
    """
    import urllib.parse as up
    q = up.quote(str(term or "").strip())
    sess = session or requests.Session()
    attempts: list[dict] = []
    last_note = ""
    proxy = SERP_HTTPS_PROXY or None

    # 本进程刚判定过「直连出口不通」→ 后续词别再撞 4 次超时，直接走已验证可用的兜底出口。
    # 兜底也失败就继续走下面的正常 4 发流程（不直接判死），让直连有机会自愈。
    if SERP_FALLBACK_READER and time.monotonic() < _STATE["direct_down_until"]:
        try:
            v = _fetch_via_reader(q, limit, sess, proxy)
            if v["ok"]:
                return {"engine": "duckduckgo", "items": v["items"],
                        "attempts": [], "via": "reader", "skipped_direct": True}
        except Exception:  # noqa: BLE001
            pass

    for step in PLAN:
        if step["wait"]:
            time.sleep(step["wait"] / 1000.0)
        # connect 超时压到 6 秒：连不上时没必要每个端点干等 20 秒
        kwargs: dict[str, Any] = {"headers": BASE_HEADERS, "timeout": (6, 15)}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        try:
            resp = sess.get(step["ep"](q), **kwargs)
            status = resp.status_code
            text = resp.text
        except Exception as exc:
            attempts.append({"engine": step["engine"], "host": step["host"],
                             "status": 0, "note": "连接失败",
                             "error": type(exc).__name__, "detail": str(exc)[:160]})
            last_note = str(exc)
            continue
        # requests.Session 会自动处理 Set-Cookie，无需手动吸收
        v = serp_verdict(status, text, limit)
        attempts.append({"engine": step["engine"], "host": step["host"], "status": status,
                         "note": v.get("note") or v.get("kind"), "items": len(v.get("items", []))})
        if v["ok"]:
            return {"engine": step["engine"], "items": v["items"], "attempts": attempts}
        last_note = v["note"]

    # 直连 4 发全没拿到真结果 → 换出口：经公共 Reader 取回原始 HTML 再判一次。
    # 本机 / 正常出口第一发就成功，永远走不到这里；只有 Render 那类连不上 DDG 的环境会用到。
    fallback_note = ""
    if SERP_FALLBACK_READER:
        try:
            v = _fetch_via_reader(q, limit, sess, proxy)
            if v["ok"]:
                _STATE["direct_down_until"] = time.monotonic() + DIRECT_RETRY_SEC
                return {"engine": "duckduckgo", "items": v["items"],
                        "attempts": attempts, "via": "reader"}
            fallback_note = f"Reader 中转 {v['kind']}"
        except Exception as exc:  # noqa: BLE001 - 兜底也挂就按源级不可用上报
            fallback_note = f"Reader 中转也连不上（{type(exc).__name__}）"

    # 这跟具体词无关，换下一个词照样失败，所以按「源级不可用」上报，让上层中止整批。
    connected = sum(1 for a in attempts if a.get("status"))
    if connected == 0:
        hosts = " / ".join(dict.fromkeys(a["host"] for a in attempts))
        kinds = "/".join(sorted({a.get("error") or "?" for a in attempts}))
        note = f"连不上 DuckDuckGo（{len(attempts)} 发全部连接失败：{hosts}；{kinds}），出口网络到 DDG 不通"
    else:
        note = f"DuckDuckGo 连续 {len(attempts)} 发未返回可用结果（最后一次：{last_note}）"
    if fallback_note:
        note += f"；{fallback_note}"
    raise SourceUnavailable("duckduckgo", note + "，过几分钟再试", attempts)
