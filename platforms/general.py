"""通用解析适配器 resolver。

覆盖：快手、西瓜视频、皮皮虾、皮皮搞笑、QQ小世界、贴吧、即刻、抖音动图。
这些平台全部走第三方 HTTP 接口，不依赖 Cookie / 签名 / 外部工具。

另外承接「抖音」的降级路径：原版抖音要靠 Cookie + a-bogus 签名，
AstrBot 侧没有配 Cookie 时，退回这里用通用接口兜一把。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..core.constants import PARSE_ENDPOINTS, ParseEndpoint
from ..core.general_adapter import GeneralLinkAdapter
from .base import ResolveResult, ResolverContext, register

# 具体是哪个平台交给 core/general_adapter 的归一化函数判断，
# 这里只负责把链接归到对应的 rule_key。
_PLATFORM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("kuaishou", r"(?:chenzhongtech|kuaishou)\.com"),
    ("ixigua", r"ixigua\.com"),
    ("pipixia", r"h5\.pipix\.com"),
    ("pipigx", r"h5\.pipigx\.com"),
    ("qq_xsj", r"s\.xsj\.qq\.com"),
    ("tieba", r"tieba\.baidu\.com"),
    ("jike", r"m\.okjike\.com"),
    ("douyin_gif", r"v\.douyin\.com"),
)


def _detect_key(link: str) -> str | None:
    """判断链接属于通用适配器下的哪个子平台。"""
    for key, pattern in _PLATFORM_PATTERNS:
        if re.search(pattern, link, re.IGNORECASE):
            return key
    return None


def _build_adapter(ctx: ResolverContext) -> GeneralLinkAdapter:
    """按用户配置构造适配器（接口列表可覆盖）。"""
    raw = ctx.conf("plugin.parse_endpoints", None)
    if not raw:
        return GeneralLinkAdapter()

    endpoints: list[ParseEndpoint] = []
    for idx, template in enumerate(raw, start=1):
        if isinstance(template, str) and "{}" in template:
            endpoints.append(ParseEndpoint(sign=idx, template=template))
    if not endpoints:
        fallback = list(PARSE_ENDPOINTS)
        logger.warning("[R插件] 配置的接口全部无效，回退到内置接口表")
        return GeneralLinkAdapter(fallback)
    return GeneralLinkAdapter(endpoints)


@register("general")
async def resolve_general(link: str, ctx: ResolverContext) -> ResolveResult:
    """通用解析。"""
    key = _detect_key(link)
    if not key:
        return ResolveResult.fail("通用", "链接不属于通用适配器覆盖的平台")

    adapter = _build_adapter(ctx)
    parsed = await adapter.parse(link, key)

    if parsed is None:
        return ResolveResult.fail("通用", "未匹配到任何平台处理器")

    if not parsed.success or not parsed.has_media:
        return ResolveResult.fail(parsed.platform, parsed.error or "所有接口均无法解析")

    return ResolveResult.ok(
        parsed.platform,
        videos=[parsed.video] if parsed.video else [],
        images=list(parsed.images or []),
        desc=parsed.desc or "",
        extra={"endpoint_sign": parsed.endpoint_sign, "rule_key": key},
    )


@register("douyin_gif")
async def resolve_douyin_gif(link: str, ctx: ResolverContext) -> ResolveResult:
    """抖音动图短链，走通用适配器。"""
    return await resolve_general(link, ctx)


@register("tieba")
async def resolve_tieba(link: str, ctx: ResolverContext) -> ResolveResult:
    return await resolve_general(link, ctx)


@register("jike")
async def resolve_jike(link: str, ctx: ResolverContext) -> ResolveResult:
    return await resolve_general(link, ctx)


# 注意：快手有自己的 resolver（platforms/kuaishou.py，SSR 直解 + 回落这里），
# 所以这里不再注册 `kuaishou`，避免注册表冲突。


@register("ixigua")
async def resolve_ixigua(link: str, ctx: ResolverContext) -> ResolveResult:
    return await resolve_general(link, ctx)


@register("pipixia")
async def resolve_pipixia(link: str, ctx: ResolverContext) -> ResolveResult:
    return await resolve_general(link, ctx)


@register("pipigx")
async def resolve_pipigx(link: str, ctx: ResolverContext) -> ResolveResult:
    return await resolve_general(link, ctx)


@register("qq_xsj")
async def resolve_qq_xsj(link: str, ctx: ResolverContext) -> ResolveResult:
    return await resolve_general(link, ctx)
