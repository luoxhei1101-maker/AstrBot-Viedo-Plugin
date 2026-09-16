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

import asyncio
import json
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star

from .core import bili_login
from .core.bili_login import QRCodeUnavailable
from .core.bili_comment import fetch_bili_comments
from .core.douyin_comment import fetch_douyin_comments
from .core.config_migrate import (
    heal as heal_config,
    migrate_cookie_fields,
)
from .core.constants import (
    AUTO_RULES,
    COMMAND_RULES,
    PlatformRule,
    build_combined_pattern,
    extract_urls,
    match_rule,
)
from .core.cookies import build_cookie
from .core.downloader import (
    MediaTooLarge,
    download_media,
    download_many,
    download_many_candidates,
)
from .core.external import describe_environment, find_tool
from .core.http import HttpError
from .core.media import MergeError, merge_dash
from .platforms import ResolveResult, call
from .platforms import names as resolver_names

# B 站 QQ 小程序的 appid（判断 Json 消息段是不是 B 站小程序卡片用）
_BILI_MINIAPP_APPID = "1109937557"


def _find_bili_link_in_messages(messages: list) -> str | None:
    """从消息组件链里提取 B 站小程序卡片的跳转链接。

    QQ 群里分享 B 站视频时，常以「小程序卡片」（``CQ:json`` 消息段）的形式
    出现，而不是链接文本。这种消息的 ``get_message_str()`` 是空串，正则匹配
    不到；但消息链里有 ``Json`` 组件。

    **真实卡片结构**（实测抓到的）：``data.meta.detail_1`` 里有 B 站 appid
    ``1109937557``，跳转链接在 ``qqdocurl`` 字段（``b23.tv/xxx`` 短链）里——
    BV 号**不直接出现**，所以不能只抠 ``BV`` 号，得把短链交出去让 B 站
    resolver 自己展开。

    返回 B 站 resolver 能直接处理的链接；找不到返回 ``None``。
    """
    for comp in messages:
        if not isinstance(comp, Comp.Json):
            continue
        data = comp.data
        if not isinstance(data, dict):
            continue
        try:
            raw = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            continue

        # 确认是 B 站小程序（appid / bilibili / b23.tv 任一命中）
        if not (
            _BILI_MINIAPP_APPID in raw
            or "bilibili" in raw.lower()
            or "b23.tv" in raw.lower()
        ):
            continue

        # 优先取 qqdocurl（b23.tv 短链）；新/旧版字段名都兼容
        meta = data.get("meta") or {}
        detail = meta.get("detail_1") or meta.get("miniapp") or {}
        if isinstance(detail, dict):
            for key in ("qqdocurl", "url", "path"):
                v = detail.get(key)
                if isinstance(v, str) and (
                    "b23.tv" in v or "bilibili.com" in v or "BV" in v
                ):
                    return v

        # 兜底：从整段 data 里抠 BV 号拼标准链接
        m = re.search(r"BV[0-9A-Za-z]{10}", raw)
        if m:
            return f"https://www.bilibili.com/video/{m.group(0)}"
    return None


class BiliMiniappFilter(CustomFilter):
    """只在消息里出现 B 站小程序卡片时命中。

    用自定义 filter 而不是 ``@filter.regex``：regex 匹配的是 ``get_message_str()``，
    而小程序卡片是纯 Json 消息段、没有文本，regex 永远匹配不到。自定义 filter
    直接检查消息组件链，并且只在命中时返回 True，不会污染其它消息的唤醒判定。
    """

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return _find_bili_link_in_messages(event.get_messages()) is not None


# 需要 event / Context 才能干活、不走 resolver 注册表的命令。
# 值是对应的方法名（用 getattr 取，避免类还没定义完就互相引用）。
_LOCAL_COMMAND_METHODS: dict[str, str] = {
    "bili_scan": "cmd_bili_scan",
    "bili_state": "cmd_bili_state",
}

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

        # 扫码登录的轮询任务。Context.register_task 已经弃用
        # （源码注释：改用 initialize() 里起后台任务），但扫码是「按需触发」的，
        # 属于每次调用各自的短任务，所以自己 create_task 并持有句柄，
        # 插件卸载时统一取消，不留野任务。
        self._bg_tasks: set[asyncio.Task] = set()

        # 作品解析结果缓存：key 是链接，value 是 (写入时间戳, 结果)。
        # 重复发同一个链接时命中缓存直接重发，跳过网络解析，2 小时过期。
        self._result_cache: dict[str, tuple[float, ResolveResult]] = {}
        self._cache_ttl: float = 2 * 3600.0

        registered = set(resolver_names())
        # 本地命令（扫码登录之类）不走 resolver 注册表，自检时要排除掉，
        # 否则会误报「规则表引用了不存在的 resolver」
        configured = (
            {r.resolver for r in AUTO_RULES}
            | {r["handler"] for r in COMMAND_RULES}
        ) - set(_LOCAL_COMMAND_METHODS)
        missing = sorted(configured - registered)
        if missing:
            # 规则表里写了但没实现，早点说出来，别等用户触发才发现
            logger.warning(f"[R插件] 规则表引用了不存在的 resolver: {', '.join(missing)}")

        logger.info(
            f"[R插件] 已加载 —— 识别规则 {len(AUTO_RULES)} 条 / "
            f"命令规则 {len(COMMAND_RULES)} 条 / 已注册 resolver {len(registered)} 个"
        )
        logger.info(f"[R插件] 外部工具环境：{describe_environment()}")

        # 配置类型自愈。必须放在日志之后：它可能要写文件，先让加载日志落盘，
        # 万一自愈出问题也能看到插件已经起来了。
        self._heal_config_types()

    async def initialize(self) -> None:
        """插件启动后调用。起一个缓存定时清理任务。"""
        task = asyncio.create_task(
            self._cache_cleanup_loop(), name="rconsole_cache_cleanup"
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ==================================================================
    # 作品缓存
    # ==================================================================

    async def _cache_cleanup_loop(self) -> None:
        """每 2 小时清一次过期缓存。"""
        while True:
            await asyncio.sleep(self._cache_ttl)
            removed = self._purge_cache()
            if removed:
                logger.info(f"[R插件] 缓存清理：移除 {removed} 条过期作品")

    def _purge_cache(self) -> int:
        """清理过期缓存，返回清理条数。"""
        now = time.time()
        expired = [
            k for k, (ts, _) in self._result_cache.items()
            if now - ts > self._cache_ttl
        ]
        for k in expired:
            self._result_cache.pop(k, None)
        return len(expired)

    def _get_cached(self, url: str) -> ResolveResult | None:
        """取缓存；命中且未过期返回结果，过期则删掉并返回 None。"""
        entry = self._result_cache.get(url)
        if not entry:
            return None
        ts, result = entry
        if time.time() - ts > self._cache_ttl:
            self._result_cache.pop(url, None)
            return None
        return result

    def _cache_result(self, url: str, result: ResolveResult) -> None:
        """写入缓存；顺便做一次惰性清理，防止内存无上限。"""
        self._result_cache[url] = (time.time(), result)
        if len(self._result_cache) > 300:
            self._purge_cache()

    def _heal_config_types(self) -> None:
        """校正配置里残留的旧类型值。

        schema 类型变过之后，老配置里的值还是旧类型，WebUI 一保存就报
        「期望是 string, 得到了 int」——用户根本没碰那个字段。详见
        ``core/config_migrate.py`` 的说明。

        先做 Cookie 逐项填写「dict -> template_list」的结构性迁移，
        再做通用类型自愈。顺序不能反：通用自愈会把 template_list 的
        旧 dict 值包成 ``[{...}]``，反而弄坏配置。
        """
        schema_path = Path(__file__).parent / "_conf_schema.json"
        if not schema_path.is_file():
            return
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[R插件] 读取配置 schema 失败，跳过自愈: {exc}")
            return

        saver = getattr(self.conf_data, "save_config", None)
        save = saver if callable(saver) else None

        # 1) Cookie 逐项填写：dict -> template_list（结构性迁移，先做）
        migrate_changes = migrate_cookie_fields(self.conf_data)

        # 2) 通用类型自愈（不在这里触发保存，等两处都处理完统一存一次）
        heal_changes = heal_config(self.conf_data, schema, save=None)

        changes = migrate_changes + heal_changes
        if not changes:
            return

        if save:
            try:
                save()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[R插件][配置自愈] 保存失败: {type(exc).__name__}: {exc}")

        logger.info(f"[R插件] 配置自愈/迁移：共 {len(changes)} 处")
        for line in changes[:8]:
            logger.info(f"    · {line}")
        if len(changes) > 8:
            logger.info(f"    · ... 另外 {len(changes) - 8} 处")

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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判定发送者是不是 AstrBot 管理员。

        主判据是 ``event.is_admin()`` —— 源码里它就是 ``self.role == "admin"``，
        而 ``role`` 由 AstrBot 的 ``waking_check`` 阶段按全局配置的
        ``admins_id`` 设置。也就是说走的是 AstrBot 官方的管理员名单，
        和 ``@filter.permission_type(PermissionType.ADMIN)`` 完全同一套语义。

        再加一层兜底：直接读一次全局 ``admins_id`` 比对 sender_id。
        某些平台的适配器如果没把 role 传下来，这一层能补上。
        """
        if event.is_admin():
            return True

        try:
            cfg = self.context.get_config()
            admins = cfg.get("admins_id") or []
        except Exception as exc:  # noqa: BLE001 - 读不到就别放行
            logger.debug(f"[R插件] 读取 admins_id 失败: {exc}")
            return False

        sender = str(event.get_sender_id())
        return sender in {str(a) for a in admins}

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
    # 入口一点五：B 站小程序卡片（QQ 群里分享的 B 站视频小程序，不是链接）
    # ==================================================================

    @filter.custom_filter(BiliMiniappFilter)
    async def on_bili_miniapp(self, event: AstrMessageEvent):
        """B 站小程序卡片 → 提取跳转链接 → 走 B 站解析。

        小程序卡片是 ``CQ:json`` 消息段，``get_message_str()`` 是空串，
        正则匹配不到，所以用自定义 filter 检查消息组件链（见
        ``BiliMiniappFilter``）。
        """
        link = _find_bili_link_in_messages(event.get_messages())
        if not link:
            return
        logger.info(f"[R插件] 识别到 B 站小程序卡片: {link[:80]}")
        async for item in self._dispatch(
            event, link, forced_resolver="bilibili", forced_name="哔哩哔哩"
        ):
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
                if cmd["admin"] and not self._is_admin(event):
                    # 拒绝并记录。默认静默（不回消息），避免向普通成员
                    # 暴露「这里有个管理员指令」，也少刷屏
                    logger.warning(
                        f"[R插件] 非管理员 {event.get_sender_id()} 尝试执行受限指令 "
                        f"{cmd['name']}（{cmd['key']}），已拒绝"
                    )
                    if not self.conf_get("plugin.silent_on_no_permission", True):
                        yield event.plain_result("❌ 该指令仅限管理员使用")
                    event.stop_event()
                    return
                handled = cmd["handler"]
                break

        if not handled:
            return

        # 需要 event / Context 的命令（扫码登录之类）本地处理
        local_method = _LOCAL_COMMAND_METHODS.get(handled)
        if local_method:
            async for item in getattr(self, local_method)(event):
                yield item
            event.stop_event()
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

            resolved_any = True

            # 先查缓存（仅自动识别的链接）：重复发同一链接、命中且未过期就直接
            # 重发，跳过网络解析。命令式规则（翻译 / AI 总结）不缓存。
            cached = self._get_cached(url) if forced_resolver is None else None
            if cached is not None:
                result = cached
                logger.info(f"[R插件] 命中缓存，直接重发: {url}")
            else:
                logger.info(f"[R插件] 解析 {platform_name or resolver}: {url}")
                result = await call(resolver, url, ctx, platform_name or resolver)
                if forced_resolver is None and result.success and result.has_media:
                    self._cache_result(url, result)

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
        """把解析结果渲染成 AstrBot 消息。

        顺序统一为「先简介（类型 + 标题 + 作者），后媒体」——用户要的是先看到
        这条作品是什么、谁发的，再看到视频/图集本身，而不是先被媒体刷屏。

        一个作品可能同时有多种媒体：抖音动图就是「多个视频 + BGM」，
        B 站合并产出的是本地视频。所以这里不是 if/elif 一路到底，
        而是依次追加。
        """
        # 识别前缀沿用原 Guoba 面板配置；原版默认空串，这里给个更直观的兜底
        prefix = str(self.conf_get("global.identifyPrefix", "") or "").strip() or "🔗 识别："
        show_desc = self.conf_get("plugin.show_desc", True)

        # ---- 纯文本类结果（AI 总结 / 翻译）----
        if result.extra.get("text_only") and result.desc:
            yield event.plain_result(f"{prefix}{result.platform}\n{result.desc}")
            return

        # ---- 先发文字简介（类型 + 标题 + 作者）----
        if show_desc:
            intro = self._build_intro(result, prefix)
            if intro:
                yield event.plain_result(intro)

        sent_media = False
        skip_images = False
        skip_videos = False

        # ---- B站 DASH 延迟合并（简介已先发出，这里才下载合并）----
        dash_merge = result.extra.get("dash_merge")
        if dash_merge and dash_merge.get("video") and dash_merge.get("audio"):
            try:
                merged = await merge_dash(
                    dash_merge["video"],
                    dash_merge["audio"],
                    tag=f"bili_{result.extra.get('bvid', 'x')}",
                )
                event.track_temporary_local_file(str(merged))
                yield event.chain_result([Comp.Video.fromFileSystem(str(merged))])
                sent_media = True
                skip_images = True  # 视频已发，封面图不再单独发
            except MergeError as exc:
                logger.warning(f"[R插件][B站] 合并失败，降级为无声视频轨: {exc}")
                try:
                    yield event.chain_result(
                        [Comp.Video.fromURL(dash_merge["video"])]
                    )
                    sent_media = True
                    skip_images = True
                except Exception as exc2:  # noqa: BLE001
                    logger.warning(f"[R插件][B站] 无声视频轨也发送失败: {exc2}")
                    yield event.plain_result(f"⚠️ B站视频发送失败：{exc}")

        # ---- 本地视频（其它平台的合并产物）----
        if result.local_videos:
            path = result.local_videos[0]
            # 登记给 AstrBot，事件结束后自动回收
            event.track_temporary_local_file(path)
            try:
                yield event.chain_result([Comp.Video.fromFileSystem(path)])
                sent_media = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 发送本地视频失败: {exc}")
                yield event.plain_result(f"⚠️ {result.platform} 视频发送失败：{exc}")

        # ---- 图像册（抖音图集，可能混排静态图与动图）----
        # 单独走一条链路：动图必须按视频发、静态图按图片发，而且顺序要和
        # 作品里一致，所以不能拆成「视频链 + 图片链」两段（那样顺序会乱）。
        if result.extra.get("album_kinds"):
            async for item in self._send_album(event, result):
                yield item
            sent_media = True
            skip_images = True
            skip_videos = True

        # ---- 视频直链 ----
        if result.videos and not skip_videos:
            send_mode = self.conf_get("plugin.send_mode", "url")
            local_path: str | None = None
            if send_mode == "download":
                local_path, oversize_msg = await self._download_video(result)
                if oversize_msg:
                    # 超限是明确结论，不再尝试直链——原版也是直接放弃
                    yield event.plain_result(oversize_msg)
                    return

            if local_path:
                event.track_temporary_local_file(local_path)
                try:
                    yield event.chain_result(
                        [Comp.Video.fromFileSystem(local_path)]
                    )
                    sent_media = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[R插件] 发送下载后的视频失败: {exc}")

            if not sent_media:
                chain = self._build_video_chain(result)
                if chain:
                    try:
                        yield event.chain_result(chain)
                        sent_media = True
                    except Exception as exc:  # noqa: BLE001 - 适配器不支持 video
                        logger.warning(
                            f"[R插件] 平台不支持发送视频，降级为文本链接: {exc}"
                        )
                if not sent_media:
                    yield event.plain_result(
                        f"{prefix}{result.platform}\n{result.videos[0]}"
                    )
                    sent_media = True

        # ---- 音频（音乐平台结果 / 抖音背景音乐）----
        if result.audios:
            for url in result.audios[:3]:
                try:
                    yield event.chain_result([Comp.Record.fromURL(url)])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[R插件] 发送语音失败，降级为链接: {exc}")
                    yield event.plain_result(url)

        # ---- 图片（图集超限走合并转发）----
        if result.images and not skip_images and not skip_videos:
            async for item in self._send_images(event, result):
                yield item
            sent_media = True

        # ---- 没有任何媒体可发时的兜底 ----
        if not sent_media and not result.images and not result.audios:
            if not (show_desc and (result.title or result.author)):
                # 简介也没发出去，把已知文字情报补上
                async for item in self._render_text_only(event, result):
                    yield item

        # ---- B站评论（附加功能，失败不拖垮主流程）----
        async for item in self._maybe_send_bili_comments(event, result):
            yield item

        # ---- 抖音评论（附加功能，失败不拖垮主流程）----
        async for item in self._maybe_send_douyin_comments(event, result):
            yield item

    async def _maybe_send_bili_comments(
        self, event: AstrMessageEvent, result: ResolveResult
    ):
        """B 站视频解析成功后，按配置抓评论并用合并转发发出来。

        评论是附加功能：平台不是 B 站、开关没开、没有 aid、抓不到，都直接
        跳过，绝不影响主流程。发出去的形态用原版截图失败时的兜底方案——
        文本合并转发（不依赖截图）。
        """
        if result.platform != "哔哩哔哩":
            return
        if not self.conf_get("bili.biliComments", False):
            return
        aid = result.extra.get("aid")
        if not aid:
            return

        limit = max(1, int(self.conf_get("bili.biliCommentCount", 5) or 5))
        cookie = self.cookie_for("bili")

        try:
            comments = await fetch_bili_comments(aid, limit=limit, cookie=cookie)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][B站评论] 抓取失败，跳过: {exc}")
            return

        if not comments:
            return

        # 昵称用评论者，QQ 号用发起解析的用户（对齐合并转发的身份规则）
        sender_uin = str(event.get_sender_id() or "")
        nodes = [
            Comp.Node([Comp.Plain(c["text"])], name=c["nickname"], uin=sender_uin)
            for c in comments
        ]
        try:
            yield event.chain_result([Comp.Nodes(nodes)])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][B站评论] 合并转发发送失败: {exc}")

    async def _maybe_send_douyin_comments(
        self, event: AstrMessageEvent, result: ResolveResult
    ):
        """抖音作品解析成功后，按配置抓评论并用合并转发发出来。

        依赖 a-bogus 签名（``core/a_bogus.py`` 用容器里的 node 生成）。评论是
        附加功能：平台不是抖音、开关没开、没有 aweme_id、node 缺失、抓不到，
        都直接跳过，绝不影响主流程。
        """
        if result.platform != "抖音":
            return
        if not self.conf_get("douyin.douyinComments", False):
            return
        aweme_id = result.extra.get("aweme_id")
        if not aweme_id:
            return

        limit = max(1, int(self.conf_get("douyin.douyinCommentCount", 5) or 5))
        cookie = self.cookie_for("douyin")

        try:
            comments = await fetch_douyin_comments(
                aweme_id, cookie=cookie, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][抖音评论] 抓取失败，跳过: {exc}")
            return

        if not comments:
            return

        sender_uin = str(event.get_sender_id() or "")
        nodes = [
            Comp.Node([Comp.Plain(c["text"])], name=c["nickname"], uin=sender_uin)
            for c in comments
        ]
        try:
            yield event.chain_result([Comp.Nodes(nodes)])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][抖音评论] 合并转发发送失败: {exc}")

    def _build_intro(self, result: ResolveResult, prefix: str) -> str:
        """简介：类型 + 标题 + 作者。三者都没有时返回空串。"""
        ctype = self._content_type(result)
        lines = [f"{prefix}{result.platform}"]
        if ctype:
            lines.append(f"类型：{ctype}")
        if result.title:
            lines.append(f"标题：{result.title}")
        if result.author:
            lines.append(f"作者：{result.author}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def _content_type(self, result: ResolveResult) -> str:
        """根据媒体字段推断作品类型。"""
        # B站 DASH 延迟合并：有 dash_merge 就是视频（videos 为空、images 是封面）
        if result.extra.get("dash_merge"):
            return "视频"
        # 抖音图集：按逐项类型判，混排时明确写「图集（含动图）」
        if result.extra.get("album_kinds"):
            kinds = result.extra["album_kinds"]
            n_anim = kinds.count("animated")
            n_still = kinds.count("still")
            if n_anim and n_still:
                return "图集（含动图）"
            if n_anim:
                return "动图"
            return "图集"
        nv = len(result.videos)
        ni = len(result.images)
        if nv > 1:
            return "动图"  # 抖音动图是一组短视频
        if nv == 1 and ni:
            return "图文"
        if nv == 1:
            return "视频"
        if ni:
            return "图集"
        if result.audios:
            return "音频"
        return ""

    async def _send_album(self, event: AstrMessageEvent, result: ResolveResult):
        """发送抖音图集 —— **静态图当图片发，动图当视频发，顺序按作品原样**。

        原版 ``processDouyinImageAlbum`` 就是这么做的：逐项看有没有视频轨，
        有就下载下来当视频发（还会用 ffmpeg 合 BGM），没有就 ``segment.image``。
        移植时为了省掉落盘和 ffmpeg，动图改成发**播放直链**（``Comp.Video.fromURL``）
        ——音轨本来就在动图的视频轨里，不是静音视频。

        发送规则（和普通图集一致）：

        - 项数不超过 ``max_images``：一条消息链按顺序发完
        - 超过阈值：先把静态图并发下载到本地，用合并转发完整发出（顺序不变）
        """
        limit = max(1, int(self.conf_get("plugin.max_images", 9) or 9))
        kinds = result.extra.get("album_kinds") or []
        n_still = kinds.count("still")
        n_anim = kinds.count("animated")

        # 动图直链：videos 里全是动图，顺序与作品一致
        anim_videos = list(result.videos)
        still_images = list(result.images)
        candidates = result.extra.get("image_candidates")

        logger.debug(
            f"[R插件][抖音] 发送图集：静态图 {n_still} 张，动图 {n_anim} 个，"
            f"发送模式={'合并转发' if len(kinds) > limit else '直发'}"
        )

        # ---- 不超过阈值：单条消息链按顺序发 ----
        if len(kinds) <= limit:
            chain = []
            # 逐项还原顺序：动图从 videos 队列取，静态图从 images 队列取
            vi = 0
            ii = 0
            for kind in kinds:
                if kind == "animated":
                    if vi < len(anim_videos):
                        try:
                            chain.append(Comp.Video.fromURL(anim_videos[vi]))
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(f"[R插件] 跳过无效动图 {anim_videos[vi]}: {exc}")
                        vi += 1
                else:
                    if ii < len(still_images):
                        try:
                            chain.append(Comp.Image.fromURL(still_images[ii]))
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(f"[R插件] 跳过无效图片 {still_images[ii]}: {exc}")
                        ii += 1
            if chain:
                yield event.chain_result(chain)
            return

        # ---- 超过阈值 ----
        if not self.conf_get("plugin.album_forward_when_exceed", True):
            # 用户关掉了转发，退回「只发前 limit 项 + 提示」
            chain = []
            vi = 0
            ii = 0
            sent = 0
            for kind in kinds:
                if sent >= limit:
                    break
                if kind == "animated" and vi < len(anim_videos):
                    try:
                        chain.append(Comp.Video.fromURL(anim_videos[vi]))
                        sent += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[R插件] 跳过无效动图: {exc}")
                    vi += 1
                elif kind == "still" and ii < len(still_images):
                    try:
                        chain.append(Comp.Image.fromURL(still_images[ii]))
                        sent += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[R插件] 跳过无效图片: {exc}")
                    ii += 1
            if chain:
                yield event.chain_result(chain)
            yield event.plain_result(f"（共 {len(kinds)} 项，只发了前 {limit} 项）")
            return

        # 并发下载静态图（抖音每张图带候选 URL，签名时效不一，逐个尝试）
        concurrency = max(1, int(self.conf_get("plugin.download_concurrency", 6) or 6))
        max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
        max_bytes = max_mb * 1024 * 1024

        still_paths: list[Path | None] = []
        if still_images:
            if (
                isinstance(candidates, list)
                and candidates
                and isinstance(candidates[0], list)
                and len(candidates) == len(still_images)
            ):
                still_paths = await download_many_candidates(
                    candidates,
                    prefix="album",
                    max_bytes=max_bytes,
                    concurrency=concurrency,
                )
            else:
                still_paths = await download_many(
                    still_images,
                    prefix="album",
                    max_bytes=max_bytes,
                    concurrency=concurrency,
                )

        # 合并转发的「发送者」用发起解析的这个用户：昵称 + QQ 号都取发送者
        node_name = (event.get_sender_name() or "").strip() or "解析结果"
        node_uin = str(event.get_sender_id() or "")

        nodes = []
        skipped = 0
        vi = 0
        ii = 0
        for kind in kinds:
            if kind == "animated":
                if vi >= len(anim_videos):
                    continue
                url = anim_videos[vi]
                vi += 1
                try:
                    nodes.append(
                        Comp.Node(
                            [Comp.Video.fromURL(url)], name=node_name, uin=node_uin
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件] 跳过无效动图 {url}: {exc}")
                    skipped += 1
            else:
                if ii >= len(still_paths):
                    continue
                path = still_paths[ii]
                ii += 1
                if path is None:
                    # 下载失败（防盗链 / 签名过期）直接跳过，别把 URL 塞进 Node——
                    # Node 转 base64 时会再下载一次，失败会拖垮整条合并转发
                    skipped += 1
                    continue
                event.track_temporary_local_file(str(path))
                img = Comp.Image.fromFileSystem(str(path))
                nodes.append(Comp.Node([img], name=node_name, uin=node_uin))

        if not nodes:
            # 全部失败，退回「直发 URL」的旧行为，至少别让用户干等
            chain = []
            for url in still_images[:limit]:
                try:
                    chain.append(Comp.Image.fromURL(url))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件] 跳过无效图片 {url}: {exc}")
            for url in anim_videos[:limit]:
                try:
                    chain.append(Comp.Video.fromURL(url))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件] 跳过无效动图 {url}: {exc}")
            if chain:
                yield event.chain_result(chain)
            return

        if skipped:
            yield event.plain_result(f"（{skipped} 项发送失败，已跳过）")
        yield event.chain_result([Comp.Nodes(nodes)])

    async def _send_images(self, event: AstrMessageEvent, result: ResolveResult):
        """发送图片列表。图集数量超过 ``max_images`` 时用合并转发完整发出。"""
        limit = max(1, int(self.conf_get("plugin.max_images", 9) or 9))
        urls = result.images
        total = len(urls)

        # ---- 不超过阈值：直接一条消息链发完 ----
        if total <= limit:
            chain = []
            for img in urls:
                try:
                    chain.append(Comp.Image.fromURL(img))
                except Exception as exc:  # noqa: BLE001 - 单张图失败不该拖垮整条
                    logger.debug(f"[R插件] 跳过无效图片 {img}: {exc}")
            if chain:
                yield event.chain_result(chain)
            return

        # ---- 超过阈值：合并转发完整发出 ----
        if not self.conf_get("plugin.album_forward_when_exceed", True):
            # 用户关掉了转发，退回「只发前 limit 张 + 提示」的旧行为
            picked = urls[:limit]
            chain = []
            for img in picked:
                try:
                    chain.append(Comp.Image.fromURL(img))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件] 跳过无效图片 {img}: {exc}")
            if chain:
                yield event.chain_result(chain)
            yield event.plain_result(f"（共 {total} 张，只发了前 {limit} 张）")
            return

        # 并发下载所有图片到本地，用本地文件构造转发节点。
        # 好处：并发（快）+ Node 内部转 base64 时不再重复走网络下载。
        concurrency = max(1, int(self.conf_get("plugin.download_concurrency", 6) or 6))
        max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
        max_bytes = max_mb * 1024 * 1024

        # 抖音图集带了候选 URL（每张图多个 CDN 节点，签名时效不一），逐个尝试，
        # 第一个能下载的用——避免某张图固定取某个 URL 时因签名失效而 403。
        candidates = result.extra.get("image_candidates")
        if (
            isinstance(candidates, list)
            and candidates
            and isinstance(candidates[0], list)
            and len(candidates) == len(urls)
        ):
            paths = await download_many_candidates(
                candidates,
                prefix="album",
                max_bytes=max_bytes,
                concurrency=concurrency,
            )
        else:
            paths = await download_many(
                urls,
                prefix="album",
                max_bytes=max_bytes,
                concurrency=concurrency,
            )

        # 合并转发的「发送者」用发起解析的这个用户：昵称 + QQ 号都取发送者
        node_name = (event.get_sender_name() or "").strip() or "解析结果"
        node_uin = str(event.get_sender_id() or "")

        nodes = []
        skipped = 0
        for url, path in zip(urls, paths):
            if path is None:
                # 下载失败（防盗链 / 链接过期等）直接跳过，不要用 URL 塞进 Node——
                # Node 转 base64 时会再下载一次，那张图再失败会拖垮整条合并转发。
                skipped += 1
                continue
            event.track_temporary_local_file(str(path))
            img = Comp.Image.fromFileSystem(str(path))
            nodes.append(Comp.Node([img], name=node_name, uin=node_uin))

        if not nodes:
            # 全部下载失败，退回「直发前 limit 张 URL」的旧行为，至少别让用户干等
            chain = []
            for img in urls[:limit]:
                try:
                    chain.append(Comp.Image.fromURL(img))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件] 跳过无效图片 {img}: {exc}")
            if chain:
                yield event.chain_result(chain)
            return

        if skipped:
            yield event.plain_result(f"（{skipped} 张下载失败，已跳过）")
        yield event.chain_result([Comp.Nodes(nodes)])

    async def _download_video(self, result: ResolveResult) -> tuple[str | None, str]:
        """下载视频到本地。

        Returns:
            ``(本地路径, 超限提示)``。下载失败时路径为 None 且提示为空
            （调用方会退回直链），超限时提示非空（调用方应直接放弃）。
        """
        video_url = result.videos[0]
        try:
            # 大小上限沿用原 Guoba 面板的 videoSizeLimit（单位 MB）
            max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
            path = await download_media(
                video_url, prefix="video", max_bytes=max_mb * 1024 * 1024
            )
            return str(path), ""
        except MediaTooLarge as exc:
            return None, f"⚠️ {result.platform} 视频过大，已跳过：{exc}"
        except (HttpError, OSError) as exc:
            logger.warning(f"[R插件] 视频下载失败，回退直发链接: {exc}")
            return None, ""

    def _build_video_chain(self, result: ResolveResult) -> list:
        """把视频直链拼成消息链。抖音动图会有多条，一次发出去。"""
        limit = max(1, int(self.conf_get("plugin.max_videos", 9) or 9))
        chain = []
        for url in result.videos[:limit]:
            try:
                chain.append(Comp.Video.fromURL(url))
            except Exception as exc:  # noqa: BLE001 - 单条失败不该拖垮整条
                logger.debug(f"[R插件] 跳过无效视频 {url}: {exc}")
        return chain

    # ==================================================================
    # B 站扫码登录（对应原插件的 #RBQ / #RBS）
    # ==================================================================

    async def cmd_bili_scan(self, event: AstrMessageEvent):
        """``#RBQ`` —— 扫码登录 B 站，拿到 Cookie 后自动写进配置。"""
        try:
            qrcode_key, qr_url, img_path = await bili_login.create_login_qrcode()
        except QRCodeUnavailable as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except HttpError as exc:
            yield event.plain_result(f"❌ 申请二维码失败：{exc}")
            return

        logger.info("[R插件][B站扫码] 已生成登录二维码")

        # 登记给 AstrBot，事件结束后自动回收
        event.track_temporary_local_file(str(img_path))
        yield event.plain_result(
            "请用 **B站手机客户端** 扫描下面的二维码登录。\n"
            "扫码后还需要在手机上点一下「确认登录」。"
        )
        try:
            yield event.image_result(str(img_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][B站扫码] 二维码图片发送失败: {exc}")
            yield event.plain_result(f"二维码图片发送失败，可手动打开：{qr_url}")

        # 轮询放到后台，不然这里会把 handler 挂住几分钟
        umo = event.unified_msg_origin
        timeout = float(self.conf_get("plugin.bili_login_timeout", 180) or 180)
        task = asyncio.create_task(
            self._bili_login_worker(qrcode_key, umo, timeout),
            name="rconsole_bili_login",
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def cmd_bili_state(self, event: AstrMessageEvent):
        """``#RBS`` —— 查当前 B 站 Cookie 的登录状态。"""
        cookie = self.cookie_for("bili")
        if not cookie:
            yield event.plain_result(
                "❌ 还没配置 B 站 Cookie。\n"
                "发 `#RBQ` 扫码登录，或在插件配置里手动填。"
            )
            return

        state = await bili_login.fetch_login_state(cookie)

        lines = ["📺 **B站账号状态**"]
        if state.get("logged_in"):
            lines.append(f"登录状态：✅ 已登录（{state.get('msg')}）")
            lines.append(f"昵称：{state.get('uname')}")
            lines.append(f"UID：{state.get('mid')}")
            lines.append(f"等级：Lv{state.get('level')}")
            lines.append(f"会员：{state.get('vip_label')}")
            lines.append(f"当前 Cookie：`{bili_login.mask_cookie(cookie)}`")
        else:
            lines.append(f"登录状态：❌ {state.get('msg')}")
            lines.append("可以发 `#RBQ` 重新扫码登录。")
        yield event.plain_result("\n".join(lines))

    async def _bili_login_worker(self, qrcode_key: str, umo: str, timeout: float) -> None:
        """后台轮询扫码结果，成功后写配置并通知用户。"""
        try:
            result = await bili_login.wait_for_login(qrcode_key, timeout=timeout)
        except asyncio.CancelledError:
            logger.info("[R插件][B站扫码] 轮询任务被取消")
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务里的异常不能让整个插件炸
            logger.error(f"[R插件][B站扫码] 轮询异常: {type(exc).__name__}: {exc}")
            await self._notify(umo, f"❌ B站扫码登录出错：{exc}")
            return

        if result.get("error"):
            await self._notify(umo, f"❌ B站扫码登录失败：{result['error']}")
            return

        saved = self._save_bili_credentials(result)

        # 顺手验一下这份 Cookie 到底有没有用，省得用户以为成功了其实没生效
        state = await bili_login.fetch_login_state(saved)
        lines = ["✅ **B站登录成功，Cookie 已写入配置**"]
        if state.get("logged_in"):
            lines.append(
                f"账号：{state.get('uname')}（UID {state.get('mid')}，"
                f"{state.get('vip_label')}，Lv{state.get('level')}）"
            )
        else:
            lines.append(f"⚠️ 但状态校验没通过：{state.get('msg')}")
        lines.append("现在发 B 站视频链接就会走登录态解析（DASH + ffmpeg 合并高清）。")
        lines.append("可用 `#RBS` 随时查看账号状态。")
        await self._notify(umo, "\n".join(lines))

    def _save_bili_credentials(self, creds: dict) -> str:
        """把扫码拿到的凭据写进插件配置。

        只写「逐项填写」那栏（``bili.biliSessDataFields``），并把「整段 Cookie」
        （``bili.biliSessData``）清空 —— 因为整段那条路优先级更高，留着旧值会把
        刚扫出来的新凭据盖掉，用户改了逐项也不生效。单一数据源，避免这种鬼打墙。

        Returns:
            组装好的 Cookie 字符串（供立刻校验用）。
        """
        keys = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
        picked = {k: creds[k] for k in keys if creds.get(k)}
        cookie = "; ".join(f"{k}={v}" for k, v in picked.items())

        try:
            bili_conf = self.conf_data.setdefault("bili", {})
            fields = bili_conf.setdefault("biliSessDataFields", [])
            if isinstance(fields, list):
                # 新版 template_list 格式：清掉旧的，按顺序写入有值的项
                fields.clear()
                for key in keys:
                    val = picked.get(key, "")
                    if val:
                        fields.append({"__template_key": key, "value": val})
            elif isinstance(fields, dict):
                # 兼容还没被迁移的旧 dict 格式
                for key in keys:
                    fields[key] = picked.get(key, "")

            old_raw = str(bili_conf.get("biliSessData") or "").strip()
            if old_raw:
                logger.info("[R插件][B站扫码] 清空旧的「整段 Cookie」，避免覆盖新凭据")
                bili_conf["biliSessData"] = ""

            saver = getattr(self.conf_data, "save_config", None)
            if callable(saver):
                saver()
                logger.info("[R插件][B站扫码] 凭据已持久化到配置文件")
            else:
                logger.warning("[R插件][B站扫码] 配置对象不支持保存，凭据仅存在于内存")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件][B站扫码] 写配置失败: {type(exc).__name__}: {exc}")

        return cookie

    async def _notify(self, umo: str, text: str) -> None:
        """向指定会话主动发消息。"""
        if not umo:
            logger.warning("[R插件] 没有 umo，无法发送主动消息")
            return
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
            if not ok:
                logger.warning(f"[R插件] 主动消息未送达（找不到会话 {umo}）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件] 主动消息发送失败: {type(exc).__name__}: {exc}")

    async def terminate(self):
        """插件卸载时调用。"""
        # 取消还在跑的扫码轮询，不留野任务
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        logger.info("[R插件] 已卸载")
