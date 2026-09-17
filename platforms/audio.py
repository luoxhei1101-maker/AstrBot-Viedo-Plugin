"""音乐平台 resolver。

移植情况：
- 网易云  <- ``constants/tools.js`` 的 NETEASE_TEMP_API（第三方直链接口）
- QQ音乐  <- QQ_MUSIC_TEMP_API
- 汽水音乐 <- QISHUI_MUSIC_TEMP_API
- 酷狗    <- **未支持**（见下）
- AppleMusic / Spotify <- 原版走外部的 freyr 服务，未移植

**这个模块只处理「分享链接 -> 音频」**（用户手里已经有链接）。
「点歌搜索」是另一条链路，在 ``core/music_search.py``。

原版的音乐模块真正复杂的地方是**扫码登录 + Cookie 保活 + 自建
NeteaseCloudMusicApi 服务**（``utils/music-platform/`` 那一套）。
这里先用第三方直链接口把"发链接能拿到歌"这个主流程跑通，
登录态和音质选择留给后续。

关于酷狗
========

原版依赖用户自建 ``kugouApiServer``（一个 kugou-api 服务）。本移植版
**不再提供这个配置项**，理由：

1. 老取直链接口 ``wwwapi.kugou.com/play/songinfo`` 现在恒返回
   ``err_code=30020``，已经失效；
2. 可用的替代（``m.kugou.com/api/v1/wechat/index``）直接返回音频流本体，
   **没有可分享的播放页/直链**，不适合"发链接"这种交付方式；
3. 要求用户为了解析酷狗链接去自建一个服务，成本与收益不成比例。

所以命中酷狗链接时给明确提示，而不是让用户去配一个配不出来的东西。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..core.constants import (
    NETEASE_TEMP_API,
    QISHUI_MUSIC_TEMP_API,
    QQ_MUSIC_TEMP_API,
)
from ..core.http import HttpError, fetch_json
from .base import ResolveResult, ResolverContext, not_ported, register


def _first_url(payload: object, depth: int = 0) -> str | None:
    """在任意嵌套结构的 JSON 里找出第一个像直链的 URL。

    第三方音乐接口的返回结构各家不同且经常改，写死字段名很容易失效，
    所以这里做结构化的启发式查找——命中率高很多。
    """
    if depth > 6:
        return None
    if isinstance(payload, str):
        if payload.startswith("http") and any(
            ext in payload.lower() for ext in (".mp3", ".flac", ".m4a", ".aac", ".wav", ".ape")
        ):
            return payload
        if payload.startswith("http") and "music" in payload.lower():
            return payload
        return None
    if isinstance(payload, dict):
        # 优先看常见的 URL 字段名
        for key in ("url", "music", "src", "link", "audio", "play_url", "song_url", "data"):
            if key in payload:
                found = _first_url(payload[key], depth + 1)
                if found:
                    return found
        for value in payload.values():
            found = _first_url(value, depth + 1)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _first_url(value, depth + 1)
            if found:
                return found
    return None


def _first_text(payload: object, keys: tuple[str, ...], depth: int = 0) -> str:
    """同理，找出第一个匹配的文本字段。"""
    if depth > 6:
        return ""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _first_text(value, keys, depth + 1)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _first_text(value, keys, depth + 1)
            if found:
                return found
    return ""


def _keyword_from_link(link: str) -> str:
    """从歌曲分享链接里抠出关键词（歌名，或 id）。"""
    # 网易云 / QQ音乐 / 汽水的分享链接都能从 path 或 query 里捞到东西
    m = re.search(r"[?&](?:id|songmid|song_id|msg|gm)=([A-Za-z0-9_-]+)", link)
    if m:
        return m.group(1)
    tail = link.rstrip("/").split("/")[-1]
    return tail.split("?")[0]


async def _resolve_by_temp_api(
    link: str, platform: str, template: str
) -> ResolveResult:
    """用第三方直链接口解析音乐。"""
    keyword = _keyword_from_link(link)
    if not keyword:
        return ResolveResult.fail(platform, "无法从链接中提取歌曲标识")

    url = template.replace("{}", keyword)
    try:
        data = await fetch_json(url, retries=1, timeout=20.0)
    except HttpError as exc:
        return ResolveResult.fail(platform, f"接口请求失败: {exc}")

    audio = _first_url(data)
    if not audio:
        return ResolveResult.fail(platform, "接口没有返回可用的音频直链")

    title = _first_text(data, ("name", "title", "song", "songname", "msg"))[:120]
    author = _first_text(data, ("singer", "artist", "author", "nick"))

    return ResolveResult.ok(
        platform,
        audios=[audio],
        title=title or keyword,
        author=author,
        extra={"keyword": keyword},
    )


# ==========================================================================
# 各平台
# ==========================================================================

_WY_RE = re.compile(r"(?:music\.163\.com|163cn\.tv)")


@register("netease")
async def resolve_netease(link: str, ctx: ResolverContext) -> ResolveResult:
    """网易云单曲。"""
    # 只处理单曲分享，歌单/专辑这类需要登录态，明确拒绝而不是返回错东西
    if "/playlist" in link or "/album" in link or "/artist" in link:
        return not_ported(
            "网易云音乐",
            "歌单 / 专辑 / 歌手页未移植",
            "NeteaseCloudMusicApi 自建服务或 Cookie",
        )
    if not _WY_RE.search(link):
        return ResolveResult.fail("网易云音乐", "不是网易云链接")

    # 链接里优先取 id 参数
    m = re.search(r"[?&]id=(\d+)", link)
    keyword = m.group(1) if m else _keyword_from_link(link)
    template = NETEASE_TEMP_API.replace("{}", keyword)

    try:
        data = await fetch_json(template, retries=1, timeout=20.0)
    except HttpError as exc:
        return ResolveResult.fail("网易云音乐", f"接口请求失败: {exc}")

    audio = _first_url(data)
    if not audio:
        return ResolveResult.fail("网易云音乐", "接口没有返回可用的音频直链（可能是 VIP 歌曲）")

    return ResolveResult.ok(
        "网易云音乐",
        audios=[audio],
        title=_first_text(data, ("name", "title", "song", "songname"))[:120],
        author=_first_text(data, ("singer", "artist", "author")),
        extra={"song_id": keyword},
    )


_QQM_RE = re.compile(r"y\.qq\.com")


@register("qqmusic")
async def resolve_qqmusic(link: str, ctx: ResolverContext) -> ResolveResult:
    """QQ 音乐单曲。"""
    if not _QQM_RE.search(link):
        return ResolveResult.fail("QQ音乐", "不是 QQ 音乐链接")

    m = re.search(r"[?&](?:songmid|songDetailId)=([A-Za-z0-9]+)", link)
    keyword = m.group(1) if m else _keyword_from_link(link)
    template = QQ_MUSIC_TEMP_API.replace("{}", keyword)

    try:
        data = await fetch_json(template, retries=1, timeout=20.0)
    except HttpError as exc:
        return ResolveResult.fail("QQ音乐", f"接口请求失败: {exc}")

    audio = _first_url(data)
    if not audio:
        return ResolveResult.fail("QQ音乐", "接口没有返回可用的音频直链")

    return ResolveResult.ok(
        "QQ音乐",
        audios=[audio],
        title=_first_text(data, ("name", "title", "song", "songname"))[:120],
        author=_first_text(data, ("singer", "artist", "author")),
        extra={"songmid": keyword},
    )


_KG_RE = re.compile(
    r"(?:t1\.kugou\.com|m\.kugou\.com/share/song\.html|www\.kugou\.com/share/|h5\.kugou\.com/v2/)"
)


@register("kugou")
async def resolve_kugou(link: str, ctx: ResolverContext) -> ResolveResult:
    """酷狗音乐 —— 本移植版不支持。

    （原因见模块 docstring：原版依赖用户自建 ``kugouApiServer``，
    而它背后的接口已失效；可用的替代又拿不到可分享的直链。）
    """
    if not _KG_RE.search(link):
        return ResolveResult.fail("酷狗音乐", "不是酷狗链接")
    logger.info("[R插件] 酷狗链接解析未支持，已跳过")
    return not_ported(
        "酷狗音乐",
        "本插件不支持解析酷狗链接",
        "酷狗老接口已失效、替代接口拿不到可分享直链，故未移植。"
        "如需听歌可用 `#点歌 歌名`（支持网易云 / QQ音乐）",
    )


@register("qishui")
async def resolve_qishui(link: str, ctx: ResolverContext) -> ResolveResult:
    """汽水音乐（抖音系）。"""
    return await _resolve_by_temp_api(link, "汽水音乐", QISHUI_MUSIC_TEMP_API)


@register("freyr")
async def resolve_freyr(link: str, ctx: ResolverContext) -> ResolveResult:
    """Apple Music / Spotify。"""
    return not_ported(
        "AM+Spotify",
        "原版通过外部 freyr 服务解析，未移植",
        "一个 freyr 服务实例（原版是 docker 跑的）",
    )
