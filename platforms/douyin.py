"""抖音 resolver。

原版的抖音解析有两条路：
1. **主路**：带 Cookie 请求 ``aweme/v1/web/aweme/detail`` —— 但要 ``a-bogus`` 签名，
   签名算法在 ``utils/a-bogus.cjs``（463 行混淆 JS），还依赖 ``cycletls`` 做 TLS 指纹伪装。
2. **兜底路（SSR）**：请求 ``iesdouyin.com/share/video/{id}/`` 分享页，
   页面里内嵌 ``window._ROUTER_DATA``，直接把视频地址挖出来。**这条路不需要 Cookie。**

移植时选了第 2 条。原因很直接：第 1 条要搬的是加密混淆代码 + TLS 指纹库，
不是"移植"是"重写"；而第 2 条是纯 HTTP + JSON，逻辑干净、可验证。
代价是拿不到评论、直播、部分高清档位。
"""

from __future__ import annotations

import json
import re

from astrbot.api import logger

from ..core.http import HttpError, expand_short_url, fetch
from .base import ResolveResult, ResolverContext, register

# 抖音视频/图文的短链
_SHORT_RE = re.compile(r"(?:v|live)\.douyin\.com/[A-Za-z0-9_-]+")
# 长链里的 id
_LONG_VIDEO_RE = re.compile(r"douyin\.com/video/(\d+)")
_LONG_NOTE_RE = re.compile(r"douyin\.com/note/(\d+)")

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _extract_router_data(html: str) -> dict | None:
    """从分享页里抠出 window._ROUTER_DATA 的 JSON。

    不能简单地按行切——JSON 里可能有 ``</script>`` 之外的任意内容，
    所以要按大括号配对来定位结尾。
    """
    marker = "window._ROUTER_DATA"
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
                    logger.debug(f"[R插件][抖音] _ROUTER_DATA 解析失败: {exc}")
                    return None
    return None


def _dig_item(router_data: dict) -> dict | None:
    """在 _ROUTER_DATA 里定位到 item_list[0]。

    抖音会随版本改变外层 key（``video_(id)/page`` / ``note_(id)/page``），
    所以这里按结构特征找，而不是写死 key。
    """
    loader = router_data.get("loaderData") or {}
    for _, page in loader.items():
        if not isinstance(page, dict):
            continue
        info = page.get("videoInfoRes")
        if isinstance(info, dict):
            items = info.get("item_list") or []
            if items:
                return items[0]
        items = page.get("item_list")
        if isinstance(items, list) and items:
            return items[0]

    # 再兜一层：全局搜 item_list
    def _walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("item_list"), list) and node["item_list"]:
                return node["item_list"][0]
            for value in node.values():
                found = _walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = _walk(value)
                if found:
                    return found
        return None

    return _walk(router_data)


def _pick_video_url(item: dict) -> list[str]:
    """从 item 里抽视频地址，按清晰度从高到低试。"""
    videos: list[str] = []
    video = item.get("video") or {}

    # play_addr 的 url_list 里通常第一个就是可用的
    for addr_key in ("play_addr", "play_addr_h264", "download_addr", "play_addr_265"):
        addr = video.get(addr_key)
        if isinstance(addr, dict):
            for url in addr.get("url_list") or []:
                if url and url not in videos:
                    videos.append(url)

    # 旧字段
    for url in (video.get("playApi") or [],):
        if url and url not in videos:
            videos.append(url)

    # 把 playwm（带水印）换成 play（无水印）是抖音的老套路
    normalized: list[str] = []
    for url in videos:
        normalized.append(url.replace("/playwm/", "/play/"))
    return normalized or videos


def _pick_images(item: dict) -> list[str]:
    """图文 / 动图帖子的图片列表。"""
    images: list[str] = []
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        for url in img.get("url_list") or []:
            if url:
                images.append(url)
                break
    return images


@register("douyin")
async def resolve_douyin(link: str, ctx: ResolverContext) -> ResolveResult:
    """抖音视频 / 图文解析（SSR 免 Cookie 路线）。"""
    aweme_id: str | None = None
    is_note = False

    m = _LONG_VIDEO_RE.search(link)
    if m:
        aweme_id = m.group(1)
    else:
        m = _LONG_NOTE_RE.search(link)
        if m:
            aweme_id = m.group(1)
            is_note = True

    if not aweme_id:
        short = _SHORT_RE.search(link)
        if short:
            expanded = await expand_short_url(f"https://{short.group(0)}")
            logger.debug(f"[R插件][抖音] 短链展开: {expanded}")
            m = _LONG_VIDEO_RE.search(expanded) or _LONG_NOTE_RE.search(expanded)
            if m:
                aweme_id = m.group(1)
                is_note = "note" in expanded
            else:
                # 展开后可能落到 discover 之类的聚合页
                m = re.search(r"/(?:video|note)/(\d+)", expanded)
                if m:
                    aweme_id = m.group(1)

    if not aweme_id:
        return ResolveResult.fail("抖音", "无法从链接中提取作品 ID（短链可能已失效）")

    # ---- 抓分享页 ----
    candidates = []
    if is_note:
        from ..core.constants import DY_SHARE_NOTE_PAGE

        candidates.append(DY_SHARE_NOTE_PAGE.format(aweme_id))
    from ..core.constants import DY_SHARE_VIDEO_PAGE

    candidates.append(DY_SHARE_VIDEO_PAGE.format(aweme_id))

    html = ""
    for url in candidates:
        try:
            body, _ = await fetch(url, headers={"User-Agent": _UA}, retries=1)
            html = body.decode("utf-8", errors="ignore")
            if "_ROUTER_DATA" in html:
                break
        except HttpError as exc:
            logger.debug(f"[R插件][抖音] 分享页抓取失败 {url}: {exc}")

    if "_ROUTER_DATA" not in html:
        return ResolveResult.fail(
            "抖音", "分享页没有返回预期内容（可能需要配置 Cookie 走签名接口）"
        )

    router_data = _extract_router_data(html)
    if not router_data:
        return ResolveResult.fail("抖音", "_ROUTER_DATA 解析失败")

    item = _dig_item(router_data)
    if not item:
        return ResolveResult.fail("抖音", "分享页里没有找到作品数据")

    videos = _pick_video_url(item)
    images = _pick_images(item)

    if not videos and not images:
        return ResolveResult.fail("抖音", "作品里没有可下载的媒体")

    desc = (item.get("desc") or "")[:300]
    author = ((item.get("author") or {}).get("nickname")) or ""
    cover = ""
    video = item.get("video") or {}
    cover_info = video.get("cover") or video.get("origin_cover") or {}
    if isinstance(cover_info, dict):
        urls = cover_info.get("url_list") or []
        cover = urls[0] if urls else ""

    # 无水印直链的常见形态是把域名换成 aweme.snssdk.com 并带上 video_id
    result_videos: list[str] = []
    video_id = video.get("vid") or video.get("video_id")
    if video_id:
        from ..core.constants import DY_TOUTIAO_INFO

        result_videos.append(DY_TOUTIAO_INFO.format(video_id))
    result_videos.extend(videos[:1])

    return ResolveResult.ok(
        "抖音",
        videos=result_videos[:1],
        images=images,
        title=desc,
        author=author,
        desc=desc,
        cover=cover,
        extra={"aweme_id": aweme_id},
    )
