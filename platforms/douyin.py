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

**第二个坑：图集里的动图**。抖音图集可以**混排**静态图和动图。动图的标志是
**该项自带视频轨**（``image.video.play_addr_h264.uri``），静态图只有
``url_list``。原版 ``processDouyinImageAlbum`` 就是逐项判断：

- 有视频轨 → 下载下来用 ``segment.video`` 发（再用 ffmpeg 合 BGM）
- 没有视频轨 → ``segment.image`` 发

早期移植版按「整条作品有没有动图」一刀切：只要有一张动图，整条就标成动图、
只发视频轨，**静态图全被丢掉**，而且静态图会被当图片发出来的观感也和"动图"
对不上。现在改成 ``core.douyin_ssr.album_items`` 逐项拆分，静态图/动图各按
各的形式发，顺序严格按作品原样（见 ``main.py`` 的 ``_send_album``）。
"""

from __future__ import annotations

from astrbot.api import logger

from ..core.constants import DY_TOUTIAO_INFO
from ..core.douyin_ssr import (
    album_items,
    music_info,
    resolve_by_ssr,
    static_image_urls,
)
from ..core.http import HttpError, expand_short_url
from .base import ResolveResult, ResolverContext, register


def _play_url(uri: str, ratio: str = "1080p") -> str:
    """拼播放地址。

    ``DY_TOUTIAO_INFO`` 模板里 ratio 写死 1080p，按偏好替换——压缩时用
    720p，和原版 ``douyinCompression`` 的行为一致。
    """
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

    # ---- 图集 / 动图（可能混排）----
    if kind == "image":
        # 逐项拆分：普通图当图片发，动图（自带视频轨）当视频发。
        # 顺序严格按作品里的排列来，不能把动图和静态图分组后再发，
        # 否则用户看到的顺序和作品对不上。
        items = album_items(aweme)
        items = [it for it in items if it["kind"] == "still" or it["video_url"]]

        if not items:
            return ResolveResult.fail("抖音", "这条作品里没有可下载的媒体")

        if compressed:
            for it in items:
                if it["kind"] == "animated":
                    it["video_url"] = _play_url(it["video_uri"], "720p")

        images: list[str] = []
        image_candidates: list[str] = []
        videos: list[str] = []
        for it in items:
            if it["kind"] == "animated":
                videos.append(it["video_url"])
            else:
                images.append(it["image_url"])
                image_candidates.append(it["image_candidates"])

        animated = any(it["kind"] == "animated" for it in items)

        extra["animated"] = animated
        extra["mixed"] = bool(videos and images)
        # 逐项类型，供发送端按顺序还原（动图视频 / 静态图）
        extra["album_kinds"] = [it["kind"] for it in items]
        if image_candidates:
            extra["image_candidates"] = image_candidates
        if animated:
            # 动图按视频发，播放地址顺序与作品一致
            extra["animated_videos"] = videos

        logger.info(
            f"[R插件][抖音] 图集解析：共 {len(items)} 项，"
            f"静态图 {len(images)} 张，动图 {len(videos)} 个"
        )

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

        if videos and not images:
            label = "抖音动图"
        elif videos and images:
            label = "抖音图集"
        else:
            label = "抖音"
        return ResolveResult.ok(
            label,
            videos=videos,
            images=images,
            audios=audios,
            title=data["desc"],
            author=data["author_nickname"],
            desc=data["desc"],
            cover=data["cover_url"],
            extra=extra,
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
