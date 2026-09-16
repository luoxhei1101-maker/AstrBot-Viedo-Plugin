"""解析器基类、结果类型与注册表。

原版每个平台的 handler 是 ``apps/tools.js`` 里的一个方法，直接 ``e.reply(...)``
把消息发出去。移植后把职责拆开了：

- **resolver**：只负责「链接 -> 媒体地址列表」，不发消息、不碰框架
- **main.py**：负责把结果渲染成 AstrBot 消息

这样做的好处是 resolver 可以脱离 AstrBot 单独测试（我也确实是这么验的）。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from astrbot.api import logger


# ==========================================================================
# 结果类型
# ==========================================================================


@dataclass
class ResolveResult:
    """统一的解析结果。

    原版各 handler 直接拼消息，字段散落各处；这里收敛成固定结构，
    由 main.py 统一决定怎么发。
    """

    platform: str
    """平台中文名，用于回复文案。"""

    success: bool = False

    videos: list[str] = field(default_factory=list)
    """视频直链，通常 0 或 1 个。"""

    local_videos: list[str] = field(default_factory=list)
    """本地视频文件路径（例如 B 站 DASH 合并后的 mp4）。

    和 ``videos`` 的区别：这里的是磁盘路径，发送前要登记给 AstrBot 做回收。
    """

    images: list[str] = field(default_factory=list)
    """图片直链列表。"""

    audios: list[str] = field(default_factory=list)
    """音频直链（音乐类解析用）。"""

    title: str = ""
    author: str = ""
    desc: str = ""
    cover: str = ""

    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_media(self) -> bool:
        return bool(self.videos or self.local_videos or self.images or self.audios)

    @classmethod
    def fail(cls, platform: str, error: str) -> ResolveResult:
        return cls(platform=platform, success=False, error=error)

    @classmethod
    def ok(cls, platform: str, **kwargs: Any) -> ResolveResult:
        return cls(platform=platform, success=True, **kwargs)


# ==========================================================================
# 上下文：resolver 通过它拿到配置和状态，但不直接依赖插件实现
# ==========================================================================


class ResolverContext(Protocol):
    """resolver 能用到的最小接口。"""

    def conf(self, key: str, default: Any = None) -> Any:
        """读配置，缺字段返回默认值。"""
        ...

    def cookie(self, platform: str) -> str:
        """读某个平台的 Cookie，没配返回空串。"""
        ...

    def tool(self, name: str) -> str | None:
        """查外部命令路径（ffmpeg / yt-dlp / ...），没有返回 None。"""
        ...

    async def llm(self, prompt: str) -> str:
        """调用 AstrBot 当前配置的 LLM，返回纯文本。

        替代原版自建的那套 OpenAI 调用（``utils/llm-util.js``）——
        用户不用再单独填 baseURL / apiKey / model。
        """
        ...


# ==========================================================================
# 注册表
# ==========================================================================

ResolverFn = Callable[..., Awaitable[ResolveResult]]

_REGISTRY: dict[str, ResolverFn] = {}


def register(name: str) -> Callable[[ResolverFn], ResolverFn]:
    """把一个 resolver 注册到表里。

    用法::

        @register("weibo")
        async def resolve_weibo(link: str, ctx: ResolverContext) -> ResolveResult:
            ...
    """

    def deco(fn: ResolverFn) -> ResolverFn:
        if name in _REGISTRY:
            logger.warning(f"[R插件] resolver 名冲突，覆盖: {name}")
        _REGISTRY[name] = fn
        return fn

    return deco


def get(name: str) -> ResolverFn | None:
    """按名字取 resolver。"""
    return _REGISTRY.get(name)


def names() -> list[str]:
    """已注册的 resolver 列表，用来做启动自检。"""
    return sorted(_REGISTRY)


async def call(name: str, link: str, ctx: ResolverContext, platform_name: str) -> ResolveResult:
    """调用 resolver，统一兜底异常。

    单个平台解析炸掉不能影响整条消息的处理，所以这里全量捕获。
    """
    fn = _REGISTRY.get(name)
    if fn is None:
        return ResolveResult.fail(
            platform_name,
            f"该平台尚未移植（resolver `{name}` 不存在）",
        )

    try:
        result = fn(link, ctx)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as exc:  # noqa: BLE001 - 插件不能被单个平台拖垮
        logger.error(f"[R插件] {platform_name} 解析异常: {type(exc).__name__}: {exc}")
        return ResolveResult.fail(platform_name, f"{type(exc).__name__}: {exc}")


def not_ported(platform: str, reason: str, need: str = "") -> ResolveResult:
    """生成一个「尚未移植」的结果，带上原因和所需条件。"""
    msg = f"尚未移植：{reason}"
    if need:
        msg += f"（需要：{need}）"
    return ResolveResult.fail(platform, msg)
