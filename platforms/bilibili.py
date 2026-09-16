"""B 站 resolver。

原版 ``apps/tools.js`` 的 ``bili`` handler 是个巨物——WBI 签名、BBDown / aria2 下载、
m4s 音视频合并、直播流、番剧、专栏、评论截图、AI 总结全塞在一起。这里分两种模式：

**未登录**（没配 SESSDATA）
    走 ``platform=html5`` 的老接口，能拿到一个 360P 的完整 mp4（自带音轨）。
    画质低，但不需要签名、不需要合并、一定能播。

**已登录**（配了 SESSDATA）
    WBI 签名请求 ``x/player/wbi/playurl``，``fnval=4048`` 拿 DASH，
    按配置画质挑视频轨、按编码偏好排序，再把视频轨 + 音频轨用容器内的 ffmpeg
    无损合并（``-c copy``）成一个 mp4。

原版还有的这些**没有搬**：BBDown / aria2 接管下载、番剧（ep）、直播录制、
专栏与动态解析、评论截图卡片、AI 总结。它们各自是独立工程量，且部分依赖
外部二进制。触发时会给明确提示。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..core.bili_wbi import signed_get_json
from ..core.constants import BILI_RESOLUTION_LIST
from ..core.http import HttpError, expand_short_url, fetch_json
from ..core.media import MergeError, ffmpeg_available, merge_dash
from .base import ResolveResult, ResolverContext, register

_BV_RE = re.compile(r"(BV[1-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_EP_RE = re.compile(r"(?:ep|ss)(\d+)", re.IGNORECASE)

_WBI_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"
_VIDEO_INFO = "https://api.bilibili.com/x/web-interface/view"

# 编码偏好 -> B 站的 codecid
# 7=AVC(H.264)  12=HEVC(H.265)  13=AV1
_CODEC_PREFERENCE: dict[str, tuple[int, ...]] = {
    "auto": (12, 13, 7),  # HEVC > AV1 > AVC，与原版 codelist 注释一致
    "hevc": (12, 7, 13),
    "av1": (13, 12, 7),
    "avc": (7, 12, 13),
}

# fnval=4048：DASH + HDR + 4K + 杜比音频 + 杜比视界 + 8K + AV1 全开
_FNVAL_DASH = 4048


def _bv_from_aid(aid: int) -> str:
    """av 号转 BV 号（B 站公开的标准算法）。"""
    table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
    xor = 177451812
    add = 8728348608
    n = (aid ^ xor) + add
    positions = [11, 10, 3, 8, 4, 6]
    chars = list("BV1  4 1 7  ")
    for i, pos in enumerate(positions):
        chars[pos] = table[(n // (58**i)) % 58]
    return "".join(chars).replace(" ", "")


async def _resolve_bvid(link: str, ctx: ResolverContext) -> str | None:
    """把各种形式的 B 站链接归一到 BV 号。"""
    m = _BV_RE.search(link)
    if m:
        return m.group(1)

    av = _AV_RE.search(link)
    if av:
        try:
            return _bv_from_aid(int(av.group(1)))
        except (ValueError, IndexError):
            return None

    if any(h in link for h in ("b23.tv", "bili2233.cn")):
        expanded = await expand_short_url(link)
        logger.debug(f"[R插件][B站] 短链展开: {link} -> {expanded}")
        m = _BV_RE.search(expanded)
        if m:
            return m.group(1)
        if _AV_RE.search(expanded):
            return await _resolve_bvid(expanded, ctx)

    return None


def _build_cookie(ctx: ResolverContext) -> str:
    """取 B 站 Cookie。

    组装逻辑（整段粘贴 / 逐项填写 / 单值补 SESSDATA= 前缀）统一在
    ``core/cookies.py`` 里做，这里只负责拿结果。
    """
    return ctx.cookie("bili").strip()


def _pick_tracks(dash: dict, codec_pref: tuple[int, ...]) -> tuple[dict | None, dict | None]:
    """从 DASH 里挑出最佳视频轨和音频轨。"""
    videos = dash.get("video") or []
    audios = list(dash.get("audio") or [])

    # 杜比音频单独一列，有的话一并纳入候选
    audios.extend(dash.get("dolby", {}).get("audio") or [])

    best_video: dict | None = None
    if videos:
        # 先按编码偏好排，再按带宽降序
        def video_rank(track: dict) -> tuple[int, int]:
            codecid = track.get("codecid", 0)
            try:
                pref = codec_pref.index(codecid)
            except ValueError:
                pref = len(codec_pref)
            # pref 越小越优先，带宽越大越优先 -> 用负数让大带宽排前面
            return (pref, -int(track.get("bandwidth") or 0))

        best_video = sorted(videos, key=video_rank)[0]

    best_audio: dict | None = None
    if audios:
        best_audio = sorted(
            audios, key=lambda t: -int(t.get("bandwidth") or 0)
        )[0]

    return best_video, best_audio


def _track_url(track: dict) -> str:
    """DASH 轨道的地址字段在不同版本里叫法不同。"""
    return track.get("baseUrl") or track.get("base_url") or ""


@register("bilibili")
async def resolve_bilibili(link: str, ctx: ResolverContext) -> ResolveResult:
    """B 站视频解析。"""
    if _EP_RE.search(link) and "bilibili.com/video" not in link:
        return ResolveResult.fail(
            "哔哩哔哩",
            "番剧（ep/ss）解析未移植，需要 SESSDATA + BBDown",
        )

    bvid = await _resolve_bvid(link, ctx)
    if not bvid:
        return ResolveResult.fail("哔哩哔哩", "无法从链接中提取 BV 号")

    cookie = _build_cookie(ctx)
    logged_in = bool(cookie)

    headers = {
        "Referer": f"https://www.bilibili.com/video/{bvid}",
        "Origin": "https://www.bilibili.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if cookie:
        headers["Cookie"] = cookie

    # ---- 1. 基本信息 ----
    try:
        info = await fetch_json(f"{_VIDEO_INFO}?bvid={bvid}", headers=headers, retries=1)
    except HttpError as exc:
        return ResolveResult.fail("哔哩哔哩", f"视频信息接口请求失败: {exc}")

    if info.get("code") != 0:
        return ResolveResult.fail(
            "哔哩哔哩", info.get("message") or f"接口返回 code={info.get('code')}"
        )

    data = info.get("data") or {}
    title = data.get("title", "")
    desc = (data.get("desc") or "")
    cover = data.get("pic", "")
    author = ((data.get("owner") or {}).get("name")) or ""
    duration = int(data.get("duration") or 0)

    # 分 P 视频默认取第一 P（原版也是这个行为）
    pages = data.get("pages") or []
    cid = pages[0].get("cid") if pages else data.get("cid")
    page_title = pages[0].get("part") if len(pages) > 1 and pages else ""

    if not cid:
        return ResolveResult.fail("哔哩哔哩", "没能取到 cid")

    # 时长限制（原版配置项 biliDuration，默认 480 秒）
    max_duration = int(ctx.conf("bili.biliDuration", 480) or 0)
    if max_duration > 0 and duration > max_duration:
        return ResolveResult.fail(
            "哔哩哔哩",
            f"视频时长 {duration}s 超过配置上限 {max_duration}s（可在配置里调整）",
        )

    base_info = {
        "bvid": bvid,
        "cid": cid,
        "duration": duration,
        "url": f"https://www.bilibili.com/video/{bvid}",
    }

    # ==================================================================
    # 模式 A：未登录 —— 走 html5 老接口拿单个 360P mp4
    # ==================================================================
    if not logged_in:
        play_url = (
            "https://api.bilibili.com/x/player/playurl"
            f"?bvid={bvid}&cid={cid}&qn=16&fnval=1&platform=html5&high_quality=1"
        )
        try:
            play = await fetch_json(play_url, headers=headers, retries=1)
        except HttpError as exc:
            logger.debug(f"[R插件][B站] 免登录 playurl 失败: {exc}")
            play = {}

        durl = ((play.get("data") or {}).get("durl")) or []
        videos = [durl[0].get("url")] if durl else []

        result = ResolveResult.ok(
            "哔哩哔哩",
            videos=videos,
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            extra={**base_info, "mode": "anonymous", "quality": "360P"},
        )
        if not videos:
            result.error = "未取到免登录直链（该视频可能限制了游客访问）"
        return result

    # ==================================================================
    # 模式 B：已登录 —— WBI 签名 + DASH + ffmpeg 合并
    # ==================================================================
    quality_name = str(ctx.conf("plugin.bili_quality_when_logged_in", "1080P") or "1080P")
    qn = BILI_RESOLUTION_LIST.get(quality_name, 80)

    codec_name = str(ctx.conf("global.videoCodec", "auto") or "auto")
    codec_pref = _CODEC_PREFERENCE.get(codec_name, _CODEC_PREFERENCE["auto"])

    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnval": _FNVAL_DASH,
        "fourk": 1,
    }

    try:
        play = await signed_get_json(_WBI_PLAYURL, params, headers=headers, retries=1)
    except HttpError as exc:
        logger.warning(f"[R插件][B站] WBI playurl 失败，降级到免登录模式: {exc}")
        return ResolveResult.ok(
            "哔哩哔哩",
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            error=f"WBI 签名请求失败: {exc}",
            extra={**base_info, "mode": "wbi-failed"},
        )

    if play.get("code") != 0:
        return ResolveResult.ok(
            "哔哩哔哩",
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            error=f"playurl 返回 code={play.get('code')} {play.get('message') or ''}".strip(),
            extra={**base_info, "mode": "wbi-error"},
        )

    play_data = play.get("data") or {}
    dash = play_data.get("dash") or {}

    if not dash:
        # 没给 DASH 就退到 durl（老格式，自带音轨）
        durl = play_data.get("durl") or []
        videos = [durl[0].get("url")] if durl else []
        return ResolveResult.ok(
            "哔哩哔哩",
            videos=videos,
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            extra={**base_info, "mode": "logged-durl"},
        )

    video_track, audio_track = _pick_tracks(dash, codec_pref)
    if not video_track:
        return ResolveResult.ok(
            "哔哩哔哩",
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            error="DASH 里没有可用的视频轨（可能是付费/大会员专属内容）",
            extra={**base_info, "mode": "no-track"},
        )

    video_url = _track_url(video_track)
    audio_url = _track_url(audio_track) if audio_track else ""

    actual_quality = {
        120: "4K", 116: "1080P60", 112: "1080P+", 80: "1080P",
        64: "720P", 32: "480P", 16: "360P",
    }.get(video_track.get("id"), f"qn={video_track.get('id')}")

    extra = {
        **base_info,
        "mode": "logged-dash",
        "quality": actual_quality,
        "codec": {7: "AVC", 12: "HEVC", 13: "AV1"}.get(video_track.get("codecid"), "?"),
        "video_size_mb": round(int(video_track.get("bandwidth") or 0) * duration / 8 / 1024 / 1024, 1)
        if duration
        else 0,
    }
    if page_title:
        extra["page_title"] = page_title

    want_merge = bool(ctx.conf("plugin.bili_merge_with_ffmpeg", True))

    # 没有音频轨或不需要合并 -> 直接发视频轨（会是无声的）
    if not audio_url or not want_merge:
        note = None
        if audio_url and not want_merge:
            note = "已按配置跳过音视频合并，发出的视频没有声音"
        elif not audio_url:
            note = "接口未返回音频轨，发出的视频可能没有声音"
        return ResolveResult.ok(
            "哔哩哔哩",
            videos=[video_url],
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            error=note,
            extra=extra,
        )

    if not ffmpeg_available():
        return ResolveResult.ok(
            "哔哩哔哩",
            videos=[video_url],
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            error="未找到 ffmpeg，无法合并音视频，发出的视频没有声音",
            extra=extra,
        )

    # ---- 合并 ----
    try:
        merged = await merge_dash(video_url, audio_url, tag=f"bili_{bvid}", headers=headers)
        extra["merged"] = True
        extra["merged_size_mb"] = round(merged.stat().st_size / 1024 / 1024, 1)
        return ResolveResult.ok(
            "哔哩哔哩",
            local_videos=[str(merged)],
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            extra=extra,
        )
    except MergeError as exc:
        logger.warning(f"[R插件][B站] 合并失败，降级为单独的视频轨: {exc}")
        return ResolveResult.ok(
            "哔哩哔哩",
            videos=[video_url],
            images=[cover] if cover else [],
            title=title,
            author=author,
            desc=desc,
            cover=cover,
            error=f"音视频合并失败，发出的视频没有声音（{exc}）",
            extra=extra,
        )
