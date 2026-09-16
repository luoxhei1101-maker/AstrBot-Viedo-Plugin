"""尚未移植的平台：占位 resolver。

写这个文件不是为了凑数，是为了**把「没搬」这件事显式化**——识别正则已经全部
搬过来了，用户发这些平台的链接时会收到一条明确的「尚未移植 + 卡在哪」，
而不是静默无响应或者报一个看不懂的异常。

每一项都标注了原版对应的实现位置和缺失的前置条件，方便后续按需补。
"""

from __future__ import annotations

from .base import ResolveResult, ResolverContext, not_ported, register

# ==========================================================================
# 需要 TLS 指纹伪装 / 外部命令行工具
# ==========================================================================


@register("tiktok")
async def resolve_tiktok(link: str, ctx: ResolverContext) -> ResolveResult:
    """TikTok。"""
    if ctx.tool("yt-dlp"):
        return not_ported(
            "TikTok",
            "已检测到 yt-dlp，但下载器封装尚未接到本平台",
            "在 platforms/external.py 里补一条 tiktok 的调用参数",
        )
    return not_ported(
        "TikTok",
        "需要 `cycletls` 做 TLS 指纹伪装 + `genVerifyFp` 生成验证参数",
        "原版 utils/tiktok.js + utils/cycletls，或本机安装 yt-dlp",
    )


_TWITTER = False


@register("twitter")
async def resolve_twitter(link: str, ctx: ResolverContext) -> ResolveResult:
    """X / Twitter。"""
    return not_ported(
        "Twitter",
        "原版用 `@the-convocation/twitter-scraper` + CycleTLS，且需要配置 X Cookie",
        "Cookie（原版配置项 xCookie）+ TLS 指纹库",
    )


@register("instagram")
async def resolve_instagram(link: str, ctx: ResolverContext) -> ResolveResult:
    """Instagram。"""
    return not_ported(
        "Instagram",
        "原版走第三方临时接口 `downloader-api.bhwa233.com`，依赖其可用性",
        "该第三方接口可用，或配置 IG Cookie 自行抓取",
    )


@register("youtube")
async def resolve_youtube(link: str, ctx: ResolverContext) -> ResolveResult:
    """YouTube。"""
    ytdlp = ctx.tool("yt-dlp")
    if ytdlp:
        return not_ported(
            "YouTube",
            f"已检测到 yt-dlp（{ytdlp}），但下载器封装尚未接到本平台",
            "在 platforms/external.py 里补一条 youtube 的调用参数",
        )
    return not_ported(
        "YouTube",
        "依赖 yt-dlp 外部命令",
        "本机安装 yt-dlp（`pipx install yt-dlp` 或容器内 pip 安装）",
    )


# ==========================================================================
# 需要 Cookie / 签名算法
# ==========================================================================


@register("xiaohongshu")
async def resolve_xiaohongshu(link: str, ctx: ResolverContext) -> ResolveResult:
    """小红书。"""
    return not_ported(
        "小红书",
        "需要 xsec_token 处理 + Cookie，原版还有 `__INITIAL_STATE__` 解析",
        "有效的小红书 Cookie（原版配置项 xiaohongshuCookie）",
    )


@register("weixin_channel")
async def resolve_weixin_channel(link: str, ctx: ResolverContext) -> ResolveResult:
    """微信视频号。"""
    cookie = ctx.cookie("weixinChannel")
    if cookie:
        return not_ported(
            "视频号",
            "元宝 Cookie 已配置，但元宝对话流程（建会话 -> 请求 -> 取结果）尚未移植",
            "补完 YUANBAO_CONVERSATION_* 三接口的调用序列",
        )
    return not_ported(
        "视频号",
        "需要腾讯元宝 Web 端 Cookie 才能拿到播放地址",
        "原版配置项 weixinChannelYuanbaoCookie",
    )


# ==========================================================================
# 私有接口待核对
# ==========================================================================


@register("zuiyou")
async def resolve_zuiyou(link: str, ctx: ResolverContext) -> ResolveResult:
    """最右。"""
    return not_ported(
        "最右",
        "原版用其私有分享接口，响应结构未核对",
        "原版 apps/tools.js 里 zuiyou handler 的接口细节",
    )


@register("bodian")
async def resolve_bodian(link: str, ctx: ResolverContext) -> ResolveResult:
    """波点音乐。"""
    return not_ported(
        "波点音乐",
        "原版 utils/bodian.js 用的接口需要单独核对",
        "原版 utils/bodian.js 的接口地址与字段映射",
    )


@register("aircraft")
async def resolve_aircraft(link: str, ctx: ResolverContext) -> ResolveResult:
    """Telegram 小飞机。"""
    return not_ported(
        "小飞机",
        "需要 Telegram API（bot token / api_id）或第三方 t.me 解析服务",
        "Telegram Bot Token 配置项",
    )


# ==========================================================================
# 管理类命令（对应原版的 permission: 'master'）
# ==========================================================================


@register("bili_scan")
async def resolve_bili_scan(link: str, ctx: ResolverContext) -> ResolveResult:
    """B 站扫码登录。"""
    return not_ported(
        "B站扫码",
        "扫码登录涉及二维码生成 + 轮询 + SESSDATA 落库，未移植",
        "原版 apps/tools.js 的 biliScan handler",
    )


@register("bili_state")
async def resolve_bili_state(link: str, ctx: ResolverContext) -> ResolveResult:
    """B 站账号状态。"""
    return not_ported("B站状态", "依赖扫码登录后的 Cookie，未移植", "先完成 B 站扫码登录")


@register("netease_status")
async def resolve_netease_status(link: str, ctx: ResolverContext) -> ResolveResult:
    """网易云账号状态。"""
    return not_ported("网易云状态", "依赖网易云扫码登录，未移植", "先完成网易云扫码登录")


@register("netease_scan")
async def resolve_netease_scan(link: str, ctx: ResolverContext) -> ResolveResult:
    """网易云扫码登录。"""
    return not_ported("网易云扫码", "扫码登录流程未移植", "原版 apps/tools.js 的 netease_scan handler")


@register("kugou_status")
async def resolve_kugou_status(link: str, ctx: ResolverContext) -> ResolveResult:
    """酷狗账号状态。"""
    return not_ported("酷狗状态", "依赖酷狗扫码登录，未移植", "先完成酷狗扫码登录")


@register("kugou_scan")
async def resolve_kugou_scan(link: str, ctx: ResolverContext) -> ResolveResult:
    """酷狗扫码登录。"""
    return not_ported("酷狗扫码", "扫码登录流程未移植", "原版 apps/tools.js 的 kugou_scan handler")
