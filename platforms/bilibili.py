"""B 站 resolver（无需登录的部分）。

原版 ``apps/tools.js`` 的 ``bili`` handler 是个巨物——WBI 签名、BBDown / aria2 下载、
m4s 音视频合并、直播流、番剧、专栏、评论截图、AI 总结……全都塞在一起。

这里移植的是**不依赖登录态**的主干：
1. 展开短链（b23.tv / bili2233.cn）拿到 BV 号
2. ``x/web-interface/view`` 取视频基本信息（标题 / 作者 / 封面 / 时长 / cid）
3. 用 ``platform=html5`` 的老接口取一个免登录的 mp4 直链

WBI 签名、BBDown、aria2、评论截图这些没有一并搬过来——它们要么要 SESSDATA，
要么要本机装一堆二进制，属独立工程量。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..core.constants import BILI_BVID_TO_CID, BILI_VIDEO_INFO
from ..core.http import HttpError, expand_short_url, fetch_json
from .base import ResolveResult, ResolverContext, register

_BV_RE = re.compile(r"(BV[1-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_EP_RE = re.compile(r"ep(\d+)", re.IGNORECASE)

# B 站接口都得带 Referer，否则 403
_BILI_HEADERS = {
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def _resolve_bvid(link: str, ctx: ResolverContext) -> str | None:
    """把各种形式的 B 站链接归一到 BV 号。"""
    m = _BV_RE.search(link)
    if m:
        return m.group(1)

    # av 号 -> BV 号
    av = _AV_RE.search(link)
    if av:
        aid = int(av.group(1))
        # 标准的 aid -> bvid 算法
        table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
        xor = 177451812
        add = 8728348608
        aid = (aid ^ xor) + add
        positions = [11, 10, 3, 8, 4, 6]
        result = list("BV1  4 1 7  ")
        for i, pos in enumerate(positions):
            result[pos] = table[(aid // (58**i)) % 58]
        return "".join(result).replace(" ", "")

    # 短链展开后再找一次
    if any(h in link for h in ("b23.tv", "bili2233.cn")):
        expanded = await expand_short_url(link)
        logger.debug(f"[R插件][B站] 短链展开: {link} -> {expanded}")
        m = _BV_RE.search(expanded)
        if m:
            return m.group(1)
        av = _AV_RE.search(expanded)
        if av:
            return await _resolve_bvid(expanded, ctx)

    return None


@register("bilibili")
async def resolve_bilibili(link: str, ctx: ResolverContext) -> ResolveResult:
    """B 站视频解析（免登录主干）。"""
    if _EP_RE.search(link):
        return ResolveResult.fail(
            "哔哩哔哩",
            "番剧（ep）解析未移植，需要 SESSDATA 与 BBDown",
        )

    bvid = await _resolve_bvid(link, ctx)
    if not bvid:
        return ResolveResult.fail("哔哩哔哩", "无法从链接中提取 BV 号")

    cookie = ctx.cookie("bili")
    headers = dict(_BILI_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    # ---- 1. 基本信息 ----
    try:
        info = await fetch_json(
            f"{BILI_VIDEO_INFO}?bvid={bvid}", headers=headers, retries=1
        )
    except HttpError as exc:
        return ResolveResult.fail("哔哩哔哩", f"视频信息接口请求失败: {exc}")

    if info.get("code") != 0:
        return ResolveResult.fail(
            "哔哩哔哩", info.get("message") or f"接口返回 code={info.get('code')}"
        )

    data = info.get("data") or {}
    title = data.get("title", "")
    desc = (data.get("desc") or "")[:300]
    cover = data.get("pic", "")
    author = ((data.get("owner") or {}).get("name")) or ""
    cid = data.get("cid")
    duration = data.get("duration") or 0

    # 分 P 视频取第一 P
    pages = data.get("pages") or []
    if pages:
        cid = pages[0].get("cid") or cid

    if not cid:
        try:
            page_list = await fetch_json(
                BILI_BVID_TO_CID.format(bvid=bvid), headers=headers, retries=1
            )
            pages = (page_list.get("data") or [])
            if pages:
                cid = pages[0].get("cid")
        except HttpError:
            pass

    if not cid:
        return ResolveResult.fail("哔哩哔哩", "没能取到 cid")

    # ---- 2. 免登录直链（platform=html5 会直接给整段 mp4）----
    videos: list[str] = []
    play_url = (
        "https://api.bilibili.com/x/player/playurl"
        f"?bvid={bvid}&cid={cid}&qn=16&fnval=1&platform=html5&high_quality=1"
    )
    try:
        play = await fetch_json(play_url, headers=headers, retries=1)
        durl = ((play.get("data") or {}).get("durl")) or []
        if durl:
            videos.append(durl[0].get("url"))
        else:
            # fnval=16 的 DASH 分支，音视频分离，取视频轨
            dash = (play.get("data") or {}).get("dash") or {}
            video_tracks = dash.get("video") or []
            if video_tracks:
                videos.append(video_tracks[0].get("baseUrl") or video_tracks[0].get("base_url"))
    except HttpError as exc:
        logger.debug(f"[R插件][B站] playurl 请求失败: {exc}")

    result = ResolveResult.ok(
        "哔哩哔哩",
        videos=videos,
        images=[cover] if cover else [],
        title=title,
        author=author,
        desc=desc,
        cover=cover,
        extra={"bvid": bvid, "cid": cid, "duration": duration, "url": f"https://www.bilibili.com/video/{bvid}"},
    )

    if not videos:
        # 拿不到直链也要把信息给出去，比整体失败有用
        result.success = True
        result.error = "未取到免登录直链（可能需要 SESSDATA 或该视频限流）"

    return result
