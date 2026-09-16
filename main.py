"""astrbot_plugin_rconsole —— Yunzai 的 rconsole-plugin 在 AstrBot 上的移植版。

原插件：https://gitee.com/kyrzy0416/rconsole-plugin （作者 zhiyu1998）

架构
====

原版把所有逻辑堆在 ``apps/tools.js``（5862 行）里，每个平台一个 handler 方法，
直接调 ``e.reply()`` 把消息发出去。移植后做了分层：

::

    filter.regex 命中
          |
    main.py  _dispatch()          平台识别 + 配置过滤 + 消息渲染
          |
    platforms/base.py  registry   按名字取 resolver
          |
    platforms/*.py                只做「链接 -> 媒体地址」，不碰框架
          |
    core/*.py                     HTTP / 外部命令 / 下载 等基础设施

这样 resolver 可以脱离 AstrBot 单独跑测试，也是部署前能验证逻辑的原因。

迁移对照（Yunzai -> AstrBot）
=============================

============================  ==========================================
Yunzai                        AstrBot
============================  ==========================================
``rule: [{reg, fnc}]``        ``@filter.regex(合并正则)``
``e.reply(x)``                ``yield event.plain_result(x)``
``segment.image(url)``        ``Comp.Image.fromURL(url)``
``segment.video(path)``       ``Comp.Video.fromFileSystem(path)``
``Bot.makeForwardMsg()``      无对应，改用多条结果
``puppeteer.screenshot()``    ``Star.html_render()``（模板需重做）
``permission: 'master'``      ``event.is_admin()``
``config/*.yaml``             ``_conf_schema.json`` + WebUI 表单
全局 ``redis``                 ``Star.get_kv_data/put_kv_data``
自建 OpenAI 调用               ``Context.get_using_provider_async()``
============================  ==========================================
"""

from __future__ import annotations

import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.constants import (
    AUTO_RULES,
    COMMAND_RULES,
    PlatformRule,
    build_combined_pattern,
    extract_urls,
    match_rule,
)
from .core.cookies import build_cookie
from .core.downloader import MediaTooLarge, download_media
from .core.external import describe_environment, find_tool
from .core.http import HttpError
from .platforms import ResolveResult, call
from .platforms import names as resolver_names

# 自动识别用的合并正则。必须是模块级常量——装饰器在类定义时求值，
# 那时候还读不到用户配置；精细开关在 handler 里再判一次。
_AUTO_PATTERN = build_combined_pattern(AUTO_RULES)

# 命令式规则另拼一条
_COMMAND_PATTERN = "|".join(f"(?:{r['pattern']})" for r in COMMAND_RULES)

# 平台 key -> 配置里的 Cookie 路径
#
# 路径对应 _conf_schema.json 的分组结构：`bili.biliSessData` 指
# 「哔哩哔哩」分组下的 biliSessData 字段。这些字段名沿用原 Guoba 面板，
# 所以从 Yunzai 迁过来的用户配置可以原样搬。
_COOKIE_FIELDS: dict[str, str] = {
    "bili": "bili.biliSessData",
    "douyin": "douyin.douyinCookie",
    "kuaishou": "other.kuaishouCookie",
    "weibo": "other.weiboCookie",
    "xiaohongshu": "other.xiaohongshuCookie",
    "miyoushe": "other.miyousheCookie",
    "weixinChannel": "other.weixinChannelYuanbaoCookie",
    "xiaoheihe": "xiaoheihe.xiaoheiheCookie",
}


class _ResolverCtx:
    """给 resolver 用的上下文，实现 ``platforms.base.ResolverContext``。

    存在的意义是把插件实例和「当前这条消息」隔开——resolver 不需要知道
    AstrBot 的任何东西，也就不会被框架绑死。
    """

    def __init__(self, plugin: Main, umo: str = "") -> None:
        self._plugin = plugin
        self._umo = umo

    def conf(self, key: str, default=None):
        return self._plugin.conf_get(key, default)

    def cookie(self, platform: str) -> str:
        return self._plugin.cookie_for(platform)

    def tool(self, name: str) -> str | None:
        return self._plugin.tool_path(name)

    async def llm(self, prompt: str) -> str:
        return await self._plugin.call_llm(prompt, self._umo)


class Main(Star):
    """插件主体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.conf_data: AstrBotConfig | dict = config or {}

        registered = set(resolver_names())
        configured = {r.resolver for r in AUTO_RULES} | {r["handler"] for r in COMMAND_RULES}
        missing = sorted(configured - registered)
        if missing:
            # 规则表里写了但没实现，早点说出来，别等用户触发才发现
            logger.warning(f"[R插件] 规则表引用了不存在的 resolver: {', '.join(missing)}")

        logger.info(
            f"[R插件] 已加载 —— 识别规则 {len(AUTO_RULES)} 条 / "
            f"命令规则 {len(COMMAND_RULES)} 条 / 已注册 resolver {len(registered)} 个"
        )
        logger.info(f"[R插件] 外部工具环境：{describe_environment()}")

    # ==================================================================
    # 配置读取
    # ==================================================================

    def conf_get(self, key: str, default=None):
        """读配置，支持 ``a.b.c`` 形式的嵌套路径。

        配置 schema 是分组的（plugin / global / bili / douyin ...），
        所以取值要能顺着分组往下走。中途任何一层缺失都回退到默认值，
        不会因为用户少配了某一组就抛异常。
        """
        node = self.conf_data
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return default if node is None else node

    def cookie_for(self, platform: str) -> str:
        """取某个平台的 Cookie 字符串。

        两种填法都支持（见 ``core/cookies.py``）：

        - 用户直接粘了一整段 —— 原样返回
        - 用户逐项填了「Cookie 逐项填写」—— 按 ``key=value; key=value`` 拼好

        没有拆解方案的平台直接读整段字段。
        """
        built = build_cookie(platform, self.conf_get)
        if built:
            return built

        field = _COOKIE_FIELDS.get(platform)
        if not field:
            return ""
        return str(self.conf_get(field, "") or "").strip()

    def tool_path(self, name: str) -> str | None:
        return find_tool(name)

    async def call_llm(self, prompt: str, umo: str = "") -> str:
        """调用 AstrBot 当前配置的 LLM。"""
        provider = None
        try:
            if umo:
                provider = await self.context.get_using_provider_async(umo=umo)
            else:
                provider = await self.context.get_using_provider_async()
        except TypeError:
            # 旧版本签名不接受 umo
            provider = await self.context.get_using_provider_async()

        if provider is None:
            raise RuntimeError("当前没有可用的 LLM Provider，请先在 WebUI 里配置模型")

        response = await provider.text_chat(prompt=prompt)
        return getattr(response, "completion_text", "") or ""

    def _enabled_keys(self) -> set[str]:
        """启用自动解析的平台集合。"""
        keys = self.conf_get("plugin.enabled_platforms", [r.key for r in AUTO_RULES])
        if not isinstance(keys, (list, tuple)):
            return {r.key for r in AUTO_RULES}
        return set(keys)

    def _blacklist(self) -> set[str]:
        """全局解析黑名单（原 Guoba 面板的 globalBlackList，值是中文平台名）。"""
        raw = self.conf_get("global.globalBlackList", []) or []
        if not isinstance(raw, (list, tuple)):
            return set()
        return {str(x) for x in raw}

    # ==================================================================
    # 入口一：自动识别分享链接
    # ==================================================================

    @filter.regex(_AUTO_PATTERN)
    async def on_share_link(self, event: AstrMessageEvent):
        """消息里出现受支持的分享链接时自动解析。

        AstrBot 的正则过滤器不受 ``wake_prefix`` 限制（见
        ``astrbot/core/star/filter/regex.py`` 的注释），所以群里不 @ 机器人
        也会命中——这一点和原版 Yunzai 的 rule 行为一致，是自动解析能成立的前提。
        """
        async for item in self._dispatch(event, event.get_message_str().strip()):
            yield item

    # ==================================================================
    # 入口二：命令式规则（#RNQ / 翻en xxx / #总结一下 ...）
    # ==================================================================

    @filter.regex(_COMMAND_PATTERN)
    async def on_command_rule(self, event: AstrMessageEvent):
        """命令式规则。"""
        text = event.get_message_str().strip()

        handled: str | None = None
        for cmd in COMMAND_RULES:
            if re.search(cmd["pattern"], text, re.IGNORECASE | re.MULTILINE):
                if cmd["admin"] and not event.is_admin():
                    yield event.plain_result("❌ 该指令需要管理员权限")
                    event.stop_event()
                    return
                handled = cmd["handler"]
                break

        if not handled:
            return

        async for item in self._dispatch(
            event, text, forced_resolver=handled, forced_name=text[:20]
        ):
            yield item

    # ==================================================================

    async def _dispatch(
        self,
        event: AstrMessageEvent,
        text: str,
        forced_resolver: str | None = None,
        forced_name: str = "",
    ):
        """统一派发：识别平台 -> 调 resolver -> 渲染消息。"""
        if not self.conf_get("plugin.enable", True):
            return

        if self.conf_get("plugin.only_group", False) and event.is_private_chat():
            return

        urls = extract_urls(text)
        if not urls:
            logger.debug("[R插件] 消息里没有找到链接")
            return

        umo = event.unified_msg_origin
        ctx = _ResolverCtx(self, umo)
        enabled = self._enabled_keys()
        blacklist = self._blacklist()
        allow_multiple = bool(self.conf_get("plugin.allow_multiple_links", False))

        resolved_any = False

        for url in urls:
            resolver = forced_resolver
            platform_name = forced_name

            if forced_resolver is None:
                candidate: PlatformRule | None = match_rule(url, AUTO_RULES)
                if not candidate or candidate.key not in enabled:
                    continue
                if candidate.name in blacklist:
                    logger.debug(f"[R插件] {candidate.name} 在全局黑名单里，跳过")
                    continue
                resolver = candidate.resolver
                platform_name = candidate.name

                # 抖音没配 Cookie 时退回通用适配器（原版也有 SSR 免 Cookie 的兜底思路）
                if candidate.key == "douyin" and not self.cookie_for("douyin"):
                    if bool(self.conf_get("douyin.douyinEnableSsrBackup", True)):
                        resolver = "douyin"
                    else:
                        resolver = "general"

            resolved_any = True
            logger.info(f"[R插件] 解析 {platform_name or resolver}: {url}")

            result = await call(resolver, url, ctx, platform_name or resolver)

            if not result.success or not result.has_media:
                if not result.success:
                    logger.warning(f"[R插件] {result.platform} 解析失败: {result.error}")
                    if result.error and self.conf_get("plugin.reply_on_error", False):
                        yield event.plain_result(f"❌ {result.platform}：{result.error}")
                else:
                    # 拿到了信息但没媒体（比如 B 站限流拿不到直链），把文字情报发出去
                    async for item in self._render_text_only(event, result):
                        yield item
                continue

            async for item in self._render(event, result):
                yield item

            if not allow_multiple:
                # 原版每个 handler 处理完就 return，一条消息只解析第一个有效链接
                break

        if resolved_any and self.conf_get("plugin.stop_on_match", True):
            # 顺序要紧：先把结果 yield 出去，再停止事件传播。
            # 反过来的话结果还没进 respond 阶段就被掐了。
            event.stop_event()

    # ==================================================================
    # 消息渲染
    # ==================================================================

    async def _render_text_only(self, event: AstrMessageEvent, result: ResolveResult):
        """没有媒体、只有文字信息时的输出（B 站取不到直链的情况）。"""
        lines = [f"🔗 {result.platform}"]
        if result.title:
            lines.append(f"标题：{result.title}")
        if result.author:
            lines.append(f"作者：{result.author}")
        if result.error:
            lines.append(f"备注：{result.error}")
        yield event.plain_result("\n".join(lines))

    async def _render(self, event: AstrMessageEvent, result: ResolveResult):
        """把解析结果渲染成 AstrBot 消息。"""
        # 识别前缀沿用原 Guoba 面板配置；原版默认空串，这里给个更直观的兜底
        prefix = str(self.conf_get("global.identifyPrefix", "") or "").strip() or "🔗 识别："
        show_desc = self.conf_get("plugin.show_desc", True)
        send_mode = self.conf_get("plugin.send_mode", "url")

        # ---- 本地视频（B 站 DASH 合并产物）----
        if result.local_videos:
            path = result.local_videos[0]
            # 登记给 AstrBot，事件结束后自动回收
            event.track_temporary_local_file(path)
            try:
                yield event.chain_result([Comp.Video.fromFileSystem(path)])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 发送本地视频失败: {exc}")
                yield event.plain_result(f"⚠️ {result.platform} 视频发送失败：{exc}")

            if show_desc and (result.title or result.desc):
                yield event.plain_result(
                    f"{prefix}{result.platform}\n{(result.title or result.desc)[:300]}"
                )
            return

        # ---- 视频直链 ----
        if result.videos:
            video_url = result.videos[0]
            sent = False

            if send_mode == "download":
                try:
                    # 大小上限沿用原 Guoba 面板的 videoSizeLimit（单位 MB）
                    max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
                    path = await download_media(
                        video_url, prefix="video", max_bytes=max_mb * 1024 * 1024
                    )
                    event.track_temporary_local_file(str(path))
                    yield event.chain_result([Comp.Video.fromFileSystem(str(path))])
                    sent = True
                except MediaTooLarge as exc:
                    yield event.plain_result(f"⚠️ {result.platform} 视频过大，已跳过：{exc}")
                    return
                except (HttpError, OSError) as exc:
                    logger.warning(f"[R插件] 视频下载失败，回退直发链接: {exc}")

            if not sent:
                try:
                    yield event.chain_result([Comp.Video.fromURL(video_url)])
                except Exception as exc:  # noqa: BLE001 - 部分平台适配器不支持 video
                    logger.warning(f"[R插件] 平台不支持发送视频，降级为文本链接: {exc}")
                    yield event.plain_result(f"{prefix}{result.platform}\n{video_url}")

            if show_desc and (result.title or result.desc):
                yield event.plain_result(
                    f"{prefix}{result.platform}\n{(result.title or result.desc)[:300]}"
                )
            return

        # ---- 音频 ----
        if result.audios:
            try:
                yield event.chain_result([Comp.Record.fromURL(result.audios[0])])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 发送语音失败，降级为链接: {exc}")
                yield event.plain_result(f"{prefix}{result.platform}\n{result.audios[0]}")
            if show_desc and result.title:
                yield event.plain_result(f"{result.title} - {result.author}".strip(" -"))
            return

        # ---- 纯文本类结果（AI 总结 / 翻译）----
        if result.extra.get("text_only") and result.desc:
            yield event.plain_result(f"{prefix}{result.platform}\n{result.desc}")
            return

        # ---- 图片 ----
        if result.images:
            limit = max(1, int(self.conf_get("plugin.max_images", 9) or 9))
            picked = result.images[:limit]

            chain = []
            for img in picked:
                try:
                    chain.append(Comp.Image.fromURL(img))
                except Exception as exc:  # noqa: BLE001 - 单张图失败不该拖垮整条
                    logger.debug(f"[R插件] 跳过无效图片 {img}: {exc}")

            if chain:
                yield event.chain_result(chain)

            if len(result.images) > limit:
                yield event.plain_result(f"（共 {len(result.images)} 张，只发了前 {limit} 张）")
            elif show_desc and result.desc:
                yield event.plain_result(result.desc[:300])

    async def terminate(self):
        """插件卸载时调用。"""
        logger.info("[R插件] 已卸载")
