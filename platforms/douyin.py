"""抖音 resolver。

分三层，职责清晰：

1. ``core/douyin_ssr.py`` —— SSR 内核（原版 ``utils/douyin.js`` 的移植）：
   抓分享页、抠 ``_ROUTER_DATA``、画质探测、匿名 ttwid。
2. **本文件** —— 把内核结果翻译成 ``ResolveResult``：判类型、挑媒体、带 BGM。
3. ``main.py`` —— 渲染成 AstrBot 消息。

为什么不做原版的「主路」（``aweme/v1/web/aweme/detail``）？
那条路要 ``a-bogus`` 签名——``utils/a-bogus.cjs`` 463 行混淆 JS，还依赖
``cycletls`` 做 TLS 指纹伪装。原版自己也知道它脆，所以写了 SSR 兜底；
这里就把 SSR 当主路用。

**一个必须说清楚的坑**：图集（``aweme_type`` 2/68/150）的
``video.play_addr`` 里装的是**背景音乐**，不是视频。早期版本把它当视频发，
用户收到的是一条指向 mp3 的"视频"。现在按 ``aweme_type`` 严格分流。
"""

from __future__ import annotations

from astrbot.api import logger

from ..core.constants import DY_TOUTIAO_INFO
from ..core.douyin_ssr import (
    animated_image_uris,
    has_animated_images,
    music_info,
    resolve_by_ssr,
    static_image_urls,
)
from ..core.http import HttpError, expand_short_url
from .base import ResolveResult, ResolverContext, register


def _play_url(uri: str, compressed: bool) -> str:
    """按分辨率偏好拼播放地址。

    ``DY_TOUTIAO_INFO`` 模板里 ratio 写死 1080p，压缩时替换成 720p
    ——和原版 ``douyinCompression`` 的行为一致。
    """
    ratio = "720p" if compressed else "1080p"
    return DY_TOUTIAO_INFO.replace("1080p", ratio).replace("{}", uri)


@register("douyin")
async def resolve_douyin(link: str, ctx: ResolverContext) -> ResolveResult:
    """抖音视频 / 图集 / 动图解析。"""
    cookie = ctx.cookie("douyin")
    compressed = bool(ctx.conf("douyin.douyinCompression", False))
    # 画质探测会多发 1-4 个 Range 请求。默认开，追求最快可以关（直接用 1080p 模板）
    probe = bool(ctx.conf("plugin.douyin_probe_quality", True))

    # 短链先展开。展开失败也不算错——SSR 内核自己也会再试一次。
    if "v.douyin.com" in link:
        try:
            expanded = await expand_short_url(link)
            if expanded and "douyin.com" in expanded:
                link = expanded
        except HttpError as exc:
            logger.debug(f"[R插件][抖音] 短链展开失败，交给 SSR 内核处理: {exc}")

    try:
        data = await resolve_by_ssr(
            link, cookie=cookie, prefer_compressed=compressed, probe=probe
        )
    except HttpError as exc:
        hint = (
            "Cookie 可能已失效，可在配置里更新或清空走免登录 SSR"
            if cookie
            else "SSR 免登录通道暂时不可用，可尝试配置抖音 Cookie"
        )
        return ResolveResult.fail("抖音", f"{exc}（{hint}）")
    except ValueError as exc:
        return ResolveResult.fail("抖音", str(exc))

    kind = data["content_type"]
    aweme = data["aweme"]
    extra = {
        "aweme_id": data["aweme_id"],
        "douyin_kind": kind,
        "canonical_url": data.get("canonical_url", ""),
    }
    if data.get("selected_ratio"):
        extra["ratio"] = data["selected_ratio"]
    if data.get("available_ratios"):
        extra["available_ratios"] = data["available_ratios"]

    # ---- 视频 ----
    if kind == "video":
        return ResolveResult.ok(
            "抖音",
            videos=[data["video_url"]],
            title=data["desc"],
            author=data["author_nickname"],
            desc=data["desc"],
            cover=data["cover_url"],
            extra={**extra, "duration": data.get("duration_seconds", 0)},
        )

    # ---- 图集 / 动图 ----
    if kind == "image":
        images = static_image_urls(aweme)
        animated = has_animated_images(aweme)

        videos: list[str] = []
        if animated:
            # 动图：每张图自带视频轨，按顺序拼成播放地址。
            # 原版这里会下载每个动图并用 ffmpeg 和 BGM 合并，代价太高；
            # 这里改成直接发直链（BGM 单独作为语音发出，见下），
            # 效果等价但不用落地磁盘。
            videos = [_play_url(uri, compressed) for uri in animated_image_uris(aweme)]

        if not images and not videos:
            return ResolveResult.fail("抖音", "这条作品里没有可下载的媒体")

        # ---- 背景音乐 ----
        audios: list[str] = []
        if bool(ctx.conf("douyin.douyinMusic", True)):
            send_type = str(ctx.conf("douyin.douyinBGMSendType", "voice") or "voice")
            if send_type == "voice":
                bgm = music_info(aweme)
                if bgm["url"]:
                    audios = [bgm["url"]]
                    extra["bgm"] = (
                        f"{bgm['title']} - {bgm['author']}".strip(" -")
                    )
            else:
                # 音乐卡片要走协议端私有接口（NapCat 之类），AstrBot 这层没有
                # 统一抽象，硬做会让插件绑死某个适配器。明确记一条日志。
                logger.debug(
                    "[R插件][抖音] 音乐卡片发送方式未移植，跳过 BGM；"
                    "改成「语音」即可发送"
                )

        label = "抖音动图" if videos else "抖音"
        return ResolveResult.ok(
            label,
            videos=videos,
            images=images,
            audios=audios,
            title=data["desc"],
            author=data["author_nickname"],
            desc=data["desc"],
            cover=data["cover_url"],
            extra={**extra, "animated": animated},
        )

    # ---- 兜底：未知类型 ----
    # 抖音偶尔会引入新的 aweme_type。此时宁可按图文发（图集至少能看到图），
    # 也不要把可能指向音乐文件的 play_addr 当视频发出去。
    images = static_image_urls(aweme)
    if images:
        logger.info(
            f"[R插件][抖音] 未知 aweme_type="
            f"{aweme.get('aweme_type')}，降级按图文处理（{len(images)} 张）"
        )
        return ResolveResult.ok(
            "抖音",
            images=images,
            title=data["desc"],
            author=data["author_nickname"],
            desc=data["desc"],
            cover=data["cover_url"],
            extra={**extra, "fallback": True},
        )

    return ResolveResult.fail(
        "抖音", f"无法识别的作品类型（aweme_type={aweme.get('aweme_type')}）"
    )
