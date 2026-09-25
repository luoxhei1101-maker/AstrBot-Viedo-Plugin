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

原版还有的这些**没有搬**：BBDown / aria2 接管下载、直播录制、专栏与动态解析、
评论截图卡片、AI 总结。它们各自是独立工程量，且部分依赖外部二进制。触发时会给
明确提示。

**番剧（ep / ss）**：原版是丢给 BBDown 下载（BBDown 自带 APP 端鉴权）。本移植版
没有 BBDown，改走「season 接口拿元信息 + 番剧单集的 ``bvid`` / ``cid`` 走普通视频
接口」——绕这一下的原因是官方的 ``pgc/player/web/playurl`` 在**大陆以外**会被地区
限制（``rights.area_limit``）压成 60 秒试看：实测洛杉矶服务器上正片 ``dash.duration``
只有 62 秒，同一集在广州家宽是 305 秒，而普通视频接口不吃这个限制。
行为对齐原版：受「番剧直接解析」开关与「番剧最大时长」限制，开关关掉时只发信息不下载。
"""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger

from ..core.bili_wbi import signed_get_json
from ..core.constants import (
    BILI_QUALITY_INDEX_TO_QN,
    BILI_QN_TO_NAME,
    BILI_RESOLUTION_LIST,
)
from ..core.http import HttpError, expand_short_url, fetch_json
from ..core.media import ffmpeg_available
from .base import ResolveResult, ResolverContext, register

_BV_RE = re.compile(r"(BV[1-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
# 番剧：ep = 单集，ss = 整季。\b 是为了别把 sleep123 之类的误判成 ep
_EP_ID_RE = re.compile(r"\bep(\d+)", re.IGNORECASE)
_SS_ID_RE = re.compile(r"\bss(\d+)", re.IGNORECASE)

_WBI_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"
_VIDEO_INFO = "https://api.bilibili.com/x/web-interface/view"
# 番剧信息（season 接口）。注意它的数据在 **result** 字段，不是 data
_PGC_SEASON = "https://api.bilibili.com/pgc/view/web/season"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

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


def _as_int(value: Any) -> int:
    """容错转 int（接口字段时不时给字符串或 None）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ms_to_seconds(value: Any) -> int:
    """番剧单集的 ``duration`` 是**毫秒**，这里转成秒。

    别想着靠数值大小来判断单位 —— 61438 当毫秒是 1 分钟，当秒是 17 小时，
    两边都说得通。番剧 season 接口这个字段**固定是毫秒**（原版也是直接 /1000），
    所以无条件除。
    """
    return _as_int(value) // 1000


async def _resolve_pgc(link: str, ctx: ResolverContext) -> dict[str, Any] | None:
    """番剧链接（ep / ss）-> 单集信息；不是番剧返回 ``None``。

    season 接口一次给全：季标题、封面、简介，以及每集的 ``bvid`` / ``cid`` /
    ``duration``（毫秒）/ 小标题。``ep`` 落在正片列表里就直接取，落在
    ``section``（PV、花絮）里也能翻出来；给的是 ``ss`` 号则取第一集。

    这里**只借 season 接口拿元信息** —— 播放地址交给主流程走普通视频接口，
    原因见模块 docstring（地区限制）。
    """
    ep_m = _EP_ID_RE.search(link)
    ss_m = _SS_ID_RE.search(link)
    if not ep_m and not ss_m:
        return None

    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    cookie = _build_cookie(ctx)
    if cookie:
        headers["Cookie"] = cookie

    ep_id = _as_int(ep_m.group(1)) if ep_m else 0
    url = (
        f"{_PGC_SEASON}?ep_id={ep_id}"
        if ep_id
        else f"{_PGC_SEASON}?season_id={_as_int(ss_m.group(1))}"
    )

    try:
        resp = await fetch_json(url, headers=headers, retries=1)
    except HttpError as exc:
        logger.warning(f"[R插件][B站] 番剧信息接口失败: {exc}")
        return None

    if resp.get("code") != 0:
        logger.warning(
            f"[R插件][B站] 番剧接口 code={resp.get('code')} {resp.get('message')}"
        )
        return None

    result = resp.get("result") or {}
    if not result:
        return None

    # 正片列表 + section（PV / 花絮 / 特典）
    episodes = list(result.get("episodes") or [])
    for sec in result.get("section") or []:
        episodes.extend(sec.get("episodes") or [])

    target = None
    if ep_id:
        target = next(
            (e for e in episodes if _as_int(e.get("id") or e.get("ep_id")) == ep_id),
            None,
        )
    if target is None:
        # ss 链接（没给 ep），或给的 ep 没对上：退到第一集
        target = next((e for e in episodes if e.get("bvid") and e.get("cid")), None)
    if target is None:
        return None

    bvid = str(target.get("bvid") or "")
    cid = target.get("cid")
    if not bvid or not cid:
        return None

    real_ep = _as_int(target.get("id") or target.get("ep_id")) or ep_id
    season_title = str(result.get("title") or "")
    ep_title = str(
        target.get("show_title")
        or target.get("long_title")
        or target.get("title")
        or ""
    )
    title = f"《{season_title}》{ep_title}".strip() if season_title else ep_title

    return {
        "ep_id": real_ep,
        "bvid": bvid,
        "cid": cid,
        "title": title or season_title or "哔哩哔哩番剧",
        "season_title": season_title,
        "ep_title": ep_title,
        "cover": str(target.get("cover") or result.get("cover") or ""),
        "desc": str(result.get("evaluate") or ""),
        "duration": _ms_to_seconds(target.get("duration")),
        "web_url": f"https://www.bilibili.com/bangumi/play/ep{real_ep}",
    }


def _bangumi_gate(pgc: dict[str, Any], ctx: ResolverContext, cookie: str) -> ResolveResult | None:
    """番剧的三道闸门（对齐原版 ``biliEpInfo`` 的行为）。

    返回 ``None`` = 放行下载；否则就是这个该回给用户的结果。

    顺序：未登录 -> 超时长 -> 没开「番剧直接解析」。超时长排在开关前面，因为它是
    硬限制（原版也是先判超限就 return）。
    """
    meta = {
        "title": pgc["title"],
        "desc": pgc["desc"],
        "cover": pgc["cover"],
    }
    extra = {
        "web_url": pgc["web_url"],
        "ep_id": pgc["ep_id"],
        "season_title": pgc["season_title"],
    }

    if not cookie:
        return ResolveResult.reject(
            "哔哩哔哩",
            "番剧需要先登录（发 `#R登录 bilibili` 扫码），未登录只显示信息不下载",
            **meta,
            extra=extra,
        )

    max_duration = _as_int(ctx.conf("bili.biliBangumiDuration", 1800))
    if max_duration > 0 and pgc["duration"] > max_duration:
        return ResolveResult.reject(
            "哔哩哔哩",
            f"番剧单集 {_fmt_duration(pgc['duration'])} 超过上限 "
            f"{_fmt_duration(max_duration)}，未下载"
            f"（可在插件配置里调整 biliBangumiDuration）",
            **meta,
            extra={**extra, "duration": pgc["duration"], "max_duration": max_duration},
        )

    if not bool(ctx.conf("bili.biliBangumiDirect", False)):
        return ResolveResult.reject(
            "哔哩哔哩",
            "未开启「番剧直接解析」，按当前配置只显示信息不下载"
            "（可在插件配置里打开 biliBangumiDirect）",
            **meta,
            extra=extra,
        )

    return None


def _build_cookie(ctx: ResolverContext) -> str:
    """取 B 站 Cookie。

    组装逻辑（整段粘贴 / 逐项填写 / 单值补 SESSDATA= 前缀）统一在
    ``core/cookies.py`` 里做，这里只负责拿结果。
    """
    return ctx.cookie("bili").strip()


def _resolve_qn(ctx: ResolverContext, *, bangumi: bool = False) -> tuple[int, str]:
    """决定请求哪个画质。

    番剧优先用原插件的 ``bili.biliBangumiResolution``（「番剧独立画质」，
    同样存的是下拉索引），普通视频用 ``bili.biliResolution``。

    Returns:
        ``(qn, 展示名)``
    """
    if bangumi:
        raw = str(ctx.conf("bili.biliBangumiResolution", "") or "").strip()
        if raw.lstrip("-").isdigit():
            qn = BILI_QUALITY_INDEX_TO_QN.get(int(raw))
            if qn:
                return qn, BILI_QN_TO_NAME.get(qn, f"qn={qn}")

    raw = str(ctx.conf("bili.biliResolution", "") or "").strip()
    if raw.lstrip("-").isdigit():
        qn = BILI_QUALITY_INDEX_TO_QN.get(int(raw))
        if qn:
            return qn, BILI_QN_TO_NAME.get(qn, f"qn={qn}")

    name = str(ctx.conf("plugin.bili_quality_when_logged_in", "1080P") or "1080P")
    return BILI_RESOLUTION_LIST.get(name, 80), name


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


def _fmt_duration(seconds: int) -> str:
    """575 -> '9分35秒'。给用户看的，别把裸秒数丢出去让人自己换算。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


@register("bilibili")
async def resolve_bilibili(link: str, ctx: ResolverContext) -> ResolveResult:
    """B 站视频 / 番剧解析。"""
    # 短链先展开一次，番剧识别和 BV 提取都用展开后的结果。
    # （``_resolve_bvid`` 只在链接仍含 b23.tv 时才展开，所以不会重复请求）
    target_link = link
    if any(h in link for h in ("b23.tv", "bili2233.cn")):
        target_link = await expand_short_url(link)
        logger.debug(f"[R插件][B站] 短链展开: {link} -> {target_link}")

    cookie = _build_cookie(ctx)
    logged_in = bool(cookie)

    # ---- 番剧（ep / ss）：元信息走 season，播放地址仍走普通视频接口 ----
    pgc = await _resolve_pgc(target_link, ctx)
    if pgc:
        blocked = _bangumi_gate(pgc, ctx, cookie)
        if blocked is not None:
            return blocked

    bvid = pgc["bvid"] if pgc else await _resolve_bvid(target_link, ctx)
    if not bvid:
        return ResolveResult.fail("哔哩哔哩", "无法从链接中提取 BV 号")

    headers = {
        "Referer": (
            f"https://www.bilibili.com/bangumi/play/ep{pgc['ep_id']}"
            if pgc
            else f"https://www.bilibili.com/video/{bvid}"
        ),
        "Origin": "https://www.bilibili.com",
        "User-Agent": _UA,
    }
    if cookie:
        headers["Cookie"] = cookie

    # ---- 1. 基本信息 ----
    if pgc:
        # 番剧的 view 接口返回不可靠（标题常只剩「第N话」），元信息直接用 season 的
        title = pgc["title"]
        desc = pgc["desc"]
        cover = pgc["cover"]
        author = ""
        duration = pgc["duration"]
        cid = pgc["cid"]
        page_title = pgc["ep_title"]
        aid = None
        pages: list = []
    else:
        try:
            info = await fetch_json(
                f"{_VIDEO_INFO}?bvid={bvid}", headers=headers, retries=1
            )
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
        aid = data.get("aid")

        # 分 P 视频默认取第一 P（原版也是这个行为）
        pages = data.get("pages") or []
        cid = pages[0].get("cid") if pages else data.get("cid")
        page_title = pages[0].get("part") if len(pages) > 1 and pages else ""

    if not cid:
        return ResolveResult.fail("哔哩哔哩", "没能取到 cid")

    # 时长限制（原版配置项 biliDuration，默认 480 秒）。
    # 用 reject() 而不是 fail()：作品信息已经拿到了，只是按配置不下载。
    # 并且把标题 / UP主 / 作品页链接一并带出去 —— 只回一句「超时长」的话，
    # 用户知道为什么不发了，却不知道该去哪看（原版就是只发文字、连链接都没有）。
    #
    # 番剧走的是**另一条上限** biliBangumiDuration（默认 1800 秒，一集 24 分钟），
    # 已经在 _bangumi_gate 里判过，这里跳过，免得拿 8 分钟去卡番剧。
    max_duration = 0 if pgc else _as_int(ctx.conf("bili.biliDuration", 480))
    if max_duration > 0 and duration > max_duration:
        watch_url = f"https://www.bilibili.com/video/{bvid}"
        # 多 P 视频对齐本插件「默认取第一 P」的行为，链接也指到第一 P
        if len(pages) > 1:
            watch_url += "?p=1"
        return ResolveResult.reject(
            "哔哩哔哩",
            f"视频时长 {_fmt_duration(duration)} 超过上限 "
            f"{_fmt_duration(max_duration)}，未下载"
            f"（可在插件配置里调整 biliDuration）",
            title=title,
            author=author,
            desc=desc,
            extra={
                # 注意发的是**作品页链接**（稳定、点开就能看），
                # 不是带签名的 CDN 媒体直链（那个几小时就过期，还没法直接播）。
                "web_url": watch_url,
                "duration": duration,
                "max_duration": max_duration,
            },
        )

    base_info = {
        "bvid": bvid,
        "cid": cid,
        "aid": aid,
        "duration": duration,
        "url": pgc["web_url"] if pgc else f"https://www.bilibili.com/video/{bvid}",
    }
    if pgc:
        base_info["ep_id"] = pgc["ep_id"]
        base_info["season_title"] = pgc["season_title"]
        base_info["is_bangumi"] = True

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
    # 画质：番剧用「番剧独立画质」，普通视频用原插件的 biliResolution，
    # 两者都退回本版自己的配置项
    qn, quality_name = _resolve_qn(ctx, bangumi=bool(pgc))
    logger.debug(f"[R插件][B站] 请求画质 {quality_name}（qn={qn}）")

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

    actual_quality = BILI_QN_TO_NAME.get(
        video_track.get("id"), f"qn={video_track.get('id')}"
    )

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

    # ---- 延迟合并：只返回 DASH 轨 URL，交给 main.py 先发简介再合并 ----
    # 对齐原版「先 reply 信息、再下载合并」的流程：标题/作者等元信息立刻
    # 返回给用户，音视频轨的下载合并（几秒）放到渲染阶段异步做，不阻塞简介。
    extra["dash_merge"] = {
        "video": video_url,
        "audio": audio_url,
    }
    return ResolveResult.ok(
        "哔哩哔哩",
        images=[cover] if cover else [],
        title=title,
        author=author,
        desc=desc,
        cover=cover,
        extra=extra,
    )
