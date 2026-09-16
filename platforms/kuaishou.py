"""快手 resolver。

原版的快手走的是通用第三方接口（``utils/general-link-adapter.js`` 的 ``ks()``），
本移植版**双路并行**：

1. **主路：网页 SSR 解析**——请求 ``www.kuaishou.com/short-video/{id}`` 页面，
   抠出内嵌的 ``window.__APOLLO_STATE__``，从里面取 ``photoUrl`` / ``coverUrl``。
   带上 Cookie 成功率更高，配了就自动带上。
2. **兜底：第三方接口**——SSR 拿不到（页面改版、触发风控）时，回到原来那条
   多接口轮换的路子。

这样快手就**不再单点依赖第三方接口**了——那几个接口的存活情况你也看到了，
默认 4 个只有 1 个活着。
"""

from __future__ import annotations

import json
import re

from astrbot.api import logger

from ..core.http import HttpError, expand_short_url, fetch
from .base import ResolveResult, ResolverContext, register

_SHORT_RE = re.compile(r"(?:v|www)\.kuaishou\.com/[A-Za-z0-9_-]+")
_LONG_RE = re.compile(
    r"(?:https?://)?(?:www|v)\.(?:kuaishou|m\.chenzhongtech)\.com/[A-Za-z\d._?%&+\-=\/#]*",
    re.IGNORECASE,
)
_FW_PHOTO_RE = re.compile(r"/fw/(?:photo|long-video)/([^/?]+)")
_SHORT_VIDEO_RE = re.compile(r"short-video/([^/?]+)")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _extract_apollo_state(html: str) -> dict | None:
    """从页面里抠出 window.__APOLLO_STATE__ 的 JSON。

    用大括号配对来定位结尾——JSON 内部字符串可能含任意字符，
    不能靠简单的正则切分。
    """
    marker = "window.__APOLLO_STATE__"
    idx = html.find(marker)
    if idx == -1:
        return None

    start = html.find("{", idx)
    if start == -1:
        return None

    depth = 0
    in_str = False
    escape = False
    for pos in range(start, len(html)):
        ch = html[pos]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : pos + 1])
                except json.JSONDecodeError as exc:
                    logger.debug(f"[R插件][快手] __APOLLO_STATE__ 解析失败: {exc}")
                    return None
    return None


def _walk_find_media(node, found: dict, depth: int = 0) -> None:
    """在 Apollo state 里递归找视频相关信息。

    不写死 key 路径——快手改过好几次外层结构，按字段特征找更耐用。
    """
    if depth > 8:
        return

    if isinstance(node, dict):
        # 视频地址的候选字段名
        for key in ("photoUrl", "playUrl", "srcNoMark", "url", "videoUrl"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith("http") and not found.get("video"):
                lowered = value.lower()
                if any(ext in lowered for ext in (".mp4", "kwimgs", "kwaicdn", "video")):
                    found["video"] = value

        for key in ("coverUrl", "coverUrls", "poster", "cover"):
            value = node.get(key)
            if not found.get("cover"):
                if isinstance(value, str) and value.startswith("http"):
                    found["cover"] = value
                elif isinstance(value, list) and value:
                    first = value[0]
                    if isinstance(first, dict):
                        found["cover"] = first.get("url") or first.get("cdnUrl") or ""
                    elif isinstance(first, str):
                        found["cover"] = first

        for key in ("caption", "title", "desc"):
            value = node.get(key)
            if isinstance(value, str) and value.strip() and not found.get("caption"):
                found["caption"] = value.strip()

        user = node.get("userName") or node.get("author")
        if isinstance(user, str) and user.strip() and not found.get("author"):
            found["author"] = user.strip()

        for value in node.values():
            _walk_find_media(value, found, depth + 1)

    elif isinstance(node, list):
        for value in node:
            _walk_find_media(value, found, depth + 1)


async def _normalize(link: str) -> str | None:
    """把各种形态的快手链接归一成短链 id 或标准页面地址。"""
    m = _LONG_RE.search(link)
    if not m:
        return None
    url = m.group(0)
    if not url.startswith("http"):
        url = "https://" + url

    # 短链先展开
    if "v.kuaishou" in url:
        url = await expand_short_url(url)

    fw = _FW_PHOTO_RE.search(url)
    if fw:
        return fw.group(1)

    sv = _SHORT_VIDEO_RE.search(url)
    if sv:
        return sv.group(1)

    return None


@register("kuaishou")
async def resolve_kuaishou(link: str, ctx: ResolverContext) -> ResolveResult:
    """快手视频解析：先试 SSR，失败回落第三方接口。"""
    photo_id = await _normalize(link)
    if not photo_id:
        return ResolveResult.fail("快手", "无法从链接中提取作品 ID")

    cookie = ctx.cookie("kuaishou")
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.kuaishou.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    # ---- 主路：SSR 页面 ----
    page_url = f"https://www.kuaishou.com/short-video/{photo_id}"
    html = ""
    try:
        body, _ = await fetch(page_url, headers=headers, retries=1, timeout=20.0)
        html = body.decode("utf-8", errors="ignore")
    except HttpError as exc:
        logger.debug(f"[R插件][快手] SSR 页面抓取失败: {exc}")

    if "__APOLLO_STATE__" in html:
        state = _extract_apollo_state(html)
        if state:
            found: dict = {}
            _walk_find_media(state, found)
            if found.get("video"):
                logger.info(f"[R插件][快手] SSR 解析成功（{'带 Cookie' if cookie else '无 Cookie'}）")
                return ResolveResult.ok(
                    "快手",
                    videos=[found["video"]],
                    images=[found["cover"]] if found.get("cover") else [],
                    desc=found.get("caption", "")[:300],
                    author=found.get("author", ""),
                    cover=found.get("cover", ""),
                    extra={"mode": "ssr", "photo_id": photo_id, "with_cookie": bool(cookie)},
                )
            logger.debug("[R插件][快手] SSR 页面里没找到媒体字段")

    # ---- 兜底：第三方接口（复用通用适配器）----
    logger.info("[R插件][快手] SSR 未取到，回落到第三方通用接口")
    from .general import resolve_general  # 延迟导入，避免循环依赖

    result = await resolve_general(link, ctx)
    if result.success:
        result.extra["mode"] = "third-party-api"
        if not cookie:
            result.extra["hint"] = "配置 kuaishou Cookie 可启用 SSR 直解，降低对第三方接口的依赖"
        return result

    return ResolveResult.fail(
        "快手",
        f"SSR 与第三方接口都未能解析（{result.error or '无更多信息'}）",
    )
