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
import base64
import json
import platform as _platform
import random
import re
import tempfile
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star

from .core import bili_login
from .core.a_bogus import close_worker as close_a_bogus_worker
from .core.bili_login import QRCodeUnavailable
from .core.bili_comment import fetch_bili_comments
from .core.douyin_comment import fetch_douyin_comments
from .core.config_migrate import (
    heal as heal_config,
    migrate_cookie_fields,
    migrate_music_config,
)
from .core.constants import (
    AUTO_RULES,
    COMMAND_RULES,
    MUSIC_COMMAND_PATTERN,
    PlatformRule,
    build_combined_pattern,
    extract_urls,
    match_rule,
)
from .core.cookies import build_cookie
from .core.downloader import (
    MediaTooLarge,
    download_media,
    download_media_candidates,
    download_many,
    download_many_candidates,
)
from .core.external import describe_environment, find_tool, run
from .core.http import HttpError, close_session as close_http_session
from .core.image_bed import transfer_url, upload_image
from .core.media import (
    MergeError,
    animated_to_mp4,
    is_animated_image,
    merge_dash,
)
from .core.music_card_image import render_song_list
from .core.music_search import (
    PLATFORM_LABELS,
    get_play_url as music_get_play_url,
    search as music_search,
    verify_audio_url as music_verify_audio,
)
from .core.music_sign_proxy import (
    DEFAULT_UPSTREAM as MUSIC_SIGN_UPSTREAM,
    SignProxy,
)
from .core.cookie_spec import (
    COOKIE_REQUIRED_ANY,
    SPEC_BY_PLATFORM,
    check_cookie,
    get_spec,
    parse_cookie_keys,
)
from .core import netease_login
from .core.cookie_status import check_all as check_all_cookies
from .core.panels import (
    render_cookie_status,
    render_menu,
    render_service_status,
)
from .core.platform_caps import (
    PlatformCaps,
    caps_for,
    describe as describe_caps,
    platform_id as caps_platform_id,
)
from .core.platform_profiles import (
    describe as describe_profiles,
    group_for as profile_group,
    is_shared as profiles_is_shared,
    save_path as profile_save_path,
    value as profile_value,
)
from .core.qq_buttons import button as qq_button
from .core.qq_buttons import inline_cmd as qq_inline_cmd
from .core.qq_buttons import keyboard as qq_keyboard
from .core.qq_buttons import link_button as qq_link_button
from .core.qq_voice import VOICE_FILE_TYPE as QQ_VOICE_FILE_TYPE
from .core.qq_voice import encode_silk as qq_encode_silk
from .core.qq_voice import silk_available as qq_silk_available
from .core.service_status import (
    collect_system,
    fetch_avatar,
    fetch_bot_name,
    self_id_of,
)
from .core import render_image
from .platforms import ResolveResult, call
from .platforms import names as resolver_names

# 各平台 Cookie 的中文名（cookie_spec 里没登记的在这里补）
_COOKIE_LABELS: dict[str, str] = {
    "bili": "哔哩哔哩",
    "douyin": "抖音",
    "kuaishou": "快手",
    "weibo": "微博",
    "xiaohongshu": "小红书",
    "miyoushe": "米游社",
    "weixinChannel": "微信视频号",
    "xiaoheihe": "小黑盒",
    "netease": "网易云音乐",
    "qqmusic": "QQ音乐",
}


# B 站 QQ 小程序的 appid（判断 Json 消息段是不是 B 站小程序卡片用）
_BILI_MINIAPP_APPID = "1109937557"

# 卡片原始数据里的 JS/HTML 转义。**必须先还原再抠链接**：
# 卡片里 URL 的分隔符常常是 `\u0026`（`&` 的 JS 转义）而不是明文 `&`，
# 抠完再还原会把 URL 从 `&` 处截断，`xsec_token` 这种关键参数就丢了。
_CARD_UNESCAPE: tuple[tuple[str, str], ...] = (
    ("&#44;", ","),
    ("&#38;", "&"),
    ("&amp;", "&"),
    ("\\u0026", "&"),
    ("\\u002F", "/"),
    ("\\u003A", ":"),
    ("\\u003D", "="),
    ("\\u003F", "?"),
    ("\\u002f", "/"),
    ("\\/", "/"),
)

# 卡片里抹掉图片/图标直链用的域名词（缩略图地址又长又没排查价值）。
#
# ⚠️ **不要只列小红书/B站的 CDN**：真实卡片里长直链的 host 五花八门
# （实测小红书图文卡片的 ``preview`` 落在 ``qq.ugcimg.cn``，图标落在
# ``open.gtimg.cn``）。漏掉一个就是几千字符刷进日志。这里按「腾讯系图片
# 域名 + 各家内容 CDN」两类列，并且额外用下面那条长度规则兜底。
_CARD_IMAGE_HOSTS = (
    r"xhscdn|hdslb|douyinpic|weibocdn|sinaimg|ugcimg|gtimg|qpic|qlogo|"
    r"img\.qq\.com|byteimg|pstatp|zhipin|hdslb\.com"
)

# 兜底：任何**带图片扩展/图片处理参数**的长 URL 也算图片
_CARD_IMAGE_HINT = r"(?:\.(?:jpg|jpeg|png|webp|gif|avif|bmp))|(?:imageView2|/w/\d+|x-oss-process)"


def _card_unescape(text: str) -> str:
    """把卡片里那几种 JS/HTML 转义还原成可读字符。"""
    for old, new in _CARD_UNESCAPE:
        text = text.replace(old, new)
    return text


def _card_text(comp, *, mask_images: bool = False) -> str:
    """把一个 ``Json`` 消息段拉平成文本（转义已还原）。

    ``mask_images`` 为真时把图片直链替换成占位符 —— 只给日志用，别让一条
    卡片的缩略图 URL 把日志刷爆。抹的时候刻意**保留 ``jumpUrl`` 这类内容
    链接**（那正是排查要看的），只砍图片/图标。
    """
    data = getattr(comp, "data", None)
    if not isinstance(data, dict):
        return ""
    try:
        text = _card_unescape(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError):
        return ""
    if mask_images:
        urls = list(extract_urls(text))
        for url in urls:
            is_image_host = re.search(_CARD_IMAGE_HOSTS, url, re.I) is not None
            is_image_hint = re.search(_CARD_IMAGE_HINT, url, re.I) is not None
            if is_image_host or is_image_hint:
                text = text.replace(url, "<图片直链已省略>")
    return text


def _card_urls(comp) -> list[str]:
    """从一个 ``Json`` 消息段里抠出所有 http(s) 链接。"""
    return [u for u in extract_urls(_card_text(comp)) if u.startswith("http")]


def _capture_card_links(event: AstrMessageEvent, comp) -> None:
    """把「含受支持链接的卡片」原文记进日志，方便抓字段。

    这一步**只记录、不发送**：QQ 分享卡片里的字段名（尤其小红书的
    ``xsec_token`` 藏在哪个 key 里）东家改西家改，写死在代码里迟早失效。
    先把原文落进日志，照着日志补规则，比对着手机截图猜字段靠谱得多。

    日志里抹掉了图片直链，否则一条卡片的缩略图地址会把日志刷爆。
    """
    text = _card_text(comp, mask_images=True)
    if not text:
        return
    logger.info(f"[R插件] 收到卡片原始数据（用于补识别规则）：{text[:6000]}")
    if len(text) > 6000:
        logger.info(f"[R插件] （卡片数据被截断，完整长度 {len(text)}）")


def _brief_names(names: list[str], limit: int = 8) -> str:
    """把一长串平台名压成一行（给 #R配置 总览用，太长了会刷屏）。"""
    if not names:
        return "（无）"
    if len(names) <= limit:
        return "、".join(names)
    return "、".join(names[:limit]) + f" 等 {len(names)} 个"


def _read_video_base64(path: Path) -> str:
    """读文件并编码成 base64 字符串。

    抽成模块级函数是为了能用 ``asyncio.to_thread`` 丢进线程池跑——
    读 + 编码是同步阻塞的，视频大的时候会卡住事件循环（见
    ``Main._video_component`` 的说明）。
    """
    import base64

    return base64.b64encode(path.read_bytes()).decode("ascii")


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


def _find_card_link(comp) -> str | None:
    """从一个 ``Json`` 卡片里找出「本插件认识的平台链接」。

    小程序卡片是 ``CQ:json`` 消息段，``get_message_str()`` 是空串，普通正则
    永远匹配不到。这里把卡片原文抠出来，直接过一遍 ``AUTO_RULES`` 的识别
    规则表 —— 以后新增平台**不用再单独为卡片写一套判断**，只要规则表里的
    域名出现在卡片里就能命中。

    ⚠️ **不要按字段名取链接**。实测两种卡片的字段位置完全不同：

    ==================  ====================  ============================
    卡片类型            链接字段              样例
    ==================  ====================  ============================
    B 站小程序          ``meta.detail_1.qqdocurl``  ``https://b23.tv/xxx``
    小红书图文分享      ``meta.news.jumpUrl``       ``https://www.xiaohongshu.com/discovery/item/...``
    ==================  ====================  ============================

    连 ``app`` 字段都不一样（B 站是 ``com.tencent.miniapp_01``，小红书是
    ``com.tencent.tuwen.lua``）。所以这里**只认「卡片里有没有我们认识的
    链接」**，不认字段名也不认 appid —— 通用且不会因为 QQ 改字段而失效。

    返回能交给对应 resolver 直接用的链接，找不到返回 ``None``。
    """
    for url in _card_urls(comp):
        rule = match_rule(url, AUTO_RULES)
        if rule is not None:
            return url
    return None


class BiliMiniappFilter(CustomFilter):
    """只在消息里出现「本插件认识的卡片」时命中。

    用自定义 filter 而不是 ``@filter.regex``：regex 匹配的是 ``get_message_str()``，
    而小程序卡片是纯 Json 消息段、没有文本，regex 永远匹配不到。自定义 filter
    直接检查消息组件链，并且只在命中时返回 True，不会污染其它消息的唤醒判定。

    名字里的 Bili 是历史遗留（最早只做了 B 站小程序），现在覆盖所有卡片类型：
    小红书、B 站、微博…只要卡片里的链接能被 ``AUTO_RULES`` 认出来。
    """

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        for comp in event.get_messages():
            if not isinstance(comp, Comp.Json):
                continue
            if _find_bili_link_in_messages([comp]) or _find_card_link(comp):
                return True
        return False


# 「多图 MD」自检用的内置图。腾讯官方文档自己的 CDN 图 —— 公网必然可达，
# 用它当基线才能区分「语法不通」和「你的图拉不到」这两种失败。
_MD_IMAGE_TEST_URL = (
    "https://qq-ai.cdn-go.cn/web/bot-docs/-/v1.32.0/assets/img/image-send.35813305.jpg"
)
# 一次最多嵌几张（QQ markdown 消息体有长度上限，别把 URL 堆爆）
_MD_IMAGE_TEST_MAX = 6

# 「MD 排版自检」的默认样本 —— 直接抄一条真实的抖音作品文案：
# 它同时具备两个会引爆 markdown 的特征：**多行** + 每行以 `#话题` 开头。
# 正常放进正文里，每一行都会被当成标题，整片排版垮掉。
_MD_LAYOUT_SAMPLE = (
    "一直说说说。#小猫 #罗小黑 #皇受 #同人 #抽象\n"
    "#暗区跳舞 #猎奇 #抽象\n"
    "原声 - 卡卡（反迷你）"
)
# 零宽空格：插在 `#` 后面能让 markdown 不把它当标题语法，肉眼看不出来。
_MD_ZWSP = "\u200b"

# 菜单随机图 API（``plugin.menuImageApi`` 的默认值）—— 用 elaina 的**竖屏**档。
#
# 实测三档的比例极差（2026-09-23，各取样 6 张）：
#
#     /random/           1.522 ~ 2.604      极差 1.082   ← 横竖混着给，会变形
#     /random/mobile     0.646 ~ 0.750      极差 0.104   ← 用这个
#     /random/pc         1.723 ~ 2.604      极差 0.880
#
# 尺寸是插件本地读出来再写进 markdown 的（所以最终不会变形），
# 这里挑竖屏只是因为**菜单图本来就更适合竖着看**。
_MENU_IMAGE_API_DEFAULT = "https://api.elaina.cat/random/mobile"

# 官机语音上传的超时（秒）。实测（2026-09-23，414KB silk）：
#
#     静置 150 秒后首次上传    8.28s   ✅
#     紧接着 20 秒后再传一次   46.36s  ✅   ← 被腾讯侧排队
#
# 而且耗时**与体积无关**（561KB 6.52s / 293KB 44.83s / 250KB 35.59s）。
# 用户点歌是零散的，所以正常命中「首次」那一档 —— 8 秒左右。
# 25 秒给了三倍余量；再长就等于让用户干等（botpy 默认 15 秒配 3 次重试是 94 秒）。
_QQ_UPLOAD_TIMEOUT = 25.0

# 低于这个耗时就算「秒失败」，值得重试一次。
#
# 区分两种失败：**秒失败**多半是瞬时错误（重试有效）；**耗满超时**则是被
# 腾讯侧排队了（重试只会更慢，还可能加剧排队）。所以只对前者重试。
_QQ_UPLOAD_FAST_FAIL = 6.0

# 需要 event / Context 才能干活、不走 resolver 注册表的命令。
# 值是对应的方法名（用 getattr 取，避免类还没定义完就互相引用）。
_LOCAL_COMMAND_METHODS: dict[str, str] = {
    "bili_scan": "cmd_bili_scan",
    "bili_state": "cmd_bili_state",
    "no_at": "cmd_no_at",
    "resolve_help": "cmd_resolve_help",
    # 点歌搜索：要读配置里的 Cookie + 按平台搜索，不适合走「链接 -> 媒体」
    # 那套 resolver 接口（resolver 的入参是 URL，而这里是关键词）。
    "music_search": "cmd_music_search",
    # 序号点播：读会话状态才能判断该不该响应，同样走本地方法。
    "music_pick": "cmd_music_pick",
    # 网易云扫码：要发二维码图 + 起后台轮询，需要 event。
    "netease_scan": "cmd_netease_scan",
    # 三个图片命令：都要 event（拿 Bot QQ 号/平台名）或读配置拼数据。
    "cookie_status": "cmd_cookie_status",
    "service_status": "cmd_service_status",
    "r_menu": "cmd_r_menu",
    # 配置管理：要读写配置、判断私聊/管理员，还要维护「等 Cookie 输入」的会话
    "r_config": "cmd_r_config",
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
    # 点歌用。网易云只要 MUSIC_U；QQ音乐要一整串（含 qqmusic_key），
    # 详见 core/music_search.py 的模块 docstring。
    # v1.3.0 起统一放在独立的 music 分组里。
    "netease": "music.neteaseCookie",
    "qqmusic": "music.qqMusicCookie",
}


# ----------------------------------------------------------------------
# #R配置 命令用的别名表
# ----------------------------------------------------------------------
#
# 用户不该为了关一个平台先去记「内部 key 是 xhs 还是 xiaohongshu」。
# 中文名、key、以及群里常用的口语叫法都收进来，输入时统一 lower() 再查。

# 平台开关：输入 -> AUTO_RULES 里的 key
_PLATFORM_ALIAS: dict[str, str] = {
    **{r.key.lower(): r.key for r in AUTO_RULES},
    **{r.name.lower(): r.key for r in AUTO_RULES},
    "b站": "bili",
    "bilibili": "bili",
    "哔哩": "bili",
    "小红书": "xhs",
    "红书": "xhs",
    "油管": "sy2b",
    "ytb": "sy2b",
    "youtube": "sy2b",
    "推特": "twitter_x",
    "x": "twitter_x",
    "twitter": "twitter_x",
    "ins": "instagram",
    "ig": "instagram",
    "视频号": "weixinChannel",
    "微信视频号": "weixinChannel",
    "贴吧": "general",
    "西瓜": "general",
    "通用": "general",
    "皮皮虾": "general",
    "网易云": "netease",
    "网易": "netease",
    "网抑云": "netease",
    "qq音乐": "qqMusic",
    "qqmusic": "qqMusic",
    "qq": "qqMusic",
    "酷狗": "kugouMusic",
    "汽水": "qishuiMusic",
    "汽水音乐": "qishuiMusic",
    "波点": "bodianMusic",
    "小飞机": "aircraft",
    "tg": "aircraft",
    "最右": "zuiyou",
    "微视": "weishi",
    "ac": "acfun",
}

# Cookie 设置：输入 -> _COOKIE_FIELDS 里的 key
#
# 注意和上面那张表**不是同一套 key**：平台开关用 AUTO_RULES 的 key（小红书是
# `xhs`），Cookie 用 _COOKIE_FIELDS 的 key（小红书是 `xiaohongshu`，沿用原
# Guoba 面板的字段名）。两张表分开才不会被这种历史差异绊倒。
_COOKIE_ALIAS: dict[str, str] = {
    "bili": "bili",
    "b站": "bili",
    "哔哩哔哩": "bili",
    "哔哩": "bili",
    "bilibili": "bili",
    "douyin": "douyin",
    "抖音": "douyin",
    "kuaishou": "kuaishou",
    "快手": "kuaishou",
    "weibo": "weibo",
    "微博": "weibo",
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "红书": "xiaohongshu",
    "miyoushe": "miyoushe",
    "米游社": "miyoushe",
    "米哈游": "miyoushe",
    "weixinchannel": "weixinChannel",
    "视频号": "weixinChannel",
    "微信视频号": "weixinChannel",
    "xiaoheihe": "xiaoheihe",
    "小黑盒": "xiaoheihe",
    "netease": "netease",
    "网易云": "netease",
    "网易": "netease",
    "网抑云": "netease",
    "qqmusic": "qqmusic",
    "qq音乐": "qqmusic",
    "qq": "qqmusic",
}

# 「#R配置」的前缀。**要求带 # 或 /**：`R配置` 这几个字落到自然语言里
# 概率不低（群里可能有人问「R配置在哪改」），不像 #R菜单 那样允许省略前缀。
_RCONFIG_PREFIX = re.compile(
    r"^[/#]{1,2}\s*(?:R配置|r配置|R设置|r设置|Rconfig|rconfig|Rc|rc)(?=\s|$)\s*",
    re.IGNORECASE,
)

# 等 Cookie 输入的会话存活时长（秒）
_COOKIE_PENDING_TTL = 180.0

# 当前插件实例。CookieInputFilter 是**类级**的自定义 filter（装饰器在类定义时
# 求值，拿不到 self），只能通过模块级引用找回插件实例。AstrBot 每个插件只实例化
# 一次，所以这里不会串台。
_CURRENT_PLUGIN: "Main | None" = None


class CookieInputFilter(CustomFilter):
    """只在「正在等管理员发 Cookie」的会话里命中。

    用自定义 filter 而不是 regex：Cookie 是一整串没有固定格式的内容，
    正则没法描述「就是它」；而按会话状态判断是精确的、也不会误伤群里的
    普通消息（等待状态只在私聊里由 ``#R配置 cookie <平台>`` 创建）。
    """

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        plugin = _CURRENT_PLUGIN
        if plugin is None:
            return False
        return plugin.has_pending_cookie(event.unified_msg_origin)


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

        # 让 CookieInputFilter 找得到本实例（见模块级 _CURRENT_PLUGIN 的说明）
        global _CURRENT_PLUGIN
        _CURRENT_PLUGIN = self

        # 「等管理员发 Cookie」的会话：umo -> (截止时间戳, Cookie 平台 key)。
        # 只有私聊里由 `#R配置 cookie <平台>` 创建，3 分钟过期。
        self._cookie_pending: dict[str, tuple[float, str]] = {}

        # 扫码登录的轮询任务。Context.register_task 已经弃用
        # （源码注释：改用 initialize() 里起后台任务），但扫码是「按需触发」的，
        # 属于每次调用各自的短任务，所以自己 create_task 并持有句柄，
        # 插件卸载时统一取消，不留野任务。
        self._bg_tasks: set[asyncio.Task] = set()

        # 音乐卡片签名代理。SnowLuma 的 music 段不自建卡片，而是 POST 给外部
        # 签名服务；而它的响应是双重编码的（外层多一层引号），SnowLuma 用
        # resp.text() 直接拿原文会导致校验失败 → 降级成残缺卡片 → QQ 回
        # 「发送者版本过低」。这里起个本地代理把响应展开一层，顺便按来源
        # 平台改写 tag/tagIcon（上游固定写「QQ音乐」）。详见
        # core/music_sign_proxy.py。
        self._sign_proxy: SignProxy | None = None

        # 序号点播会话：umo -> (写入时间戳, 关键词, 平台标签, 平台 key, 歌曲列表)
        # 用户搜索后 60 秒内回数字即播放对应歌（见 _MUSIC_PICK_TTL）。
        self._music_sessions: dict[str, tuple[float, str, str, str, list]] = {}

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
        """插件启动后调用。起缓存清理任务 + 音乐签名代理。"""
        task = asyncio.create_task(
            self._cache_cleanup_loop(), name="rconsole_cache_cleanup"
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        self._log_disabled_platforms()
        await self._start_sign_proxy()

    def _log_disabled_platforms(self) -> None:
        """启动时汇总「已支持但没勾选」的平台。

        「某平台的链接发进去没反应」是最常见的反馈，九成是
        ``plugin.enabled_platforms`` 没勾上。以前只有真的发了那条链接才会在
        日志里留一行跳过记录，事后排查很费劲；这里启动时一次说清楚。
        """
        enabled = self._enabled_keys()
        missing = [r.name for r in AUTO_RULES if r.key not in enabled]
        if not missing:
            logger.info(f"[R插件] 全部 {len(AUTO_RULES)} 个平台都已启用自动解析")
            return
        logger.info(
            f"[R插件] 未启用自动解析的平台（{len(missing)}/{len(AUTO_RULES)}）："
            f"{'、'.join(missing)}"
        )
        logger.info(
            "[R插件] 想启用哪个，在聊天里发「#R配置 平台 <平台名> 开」；"
            "也可以在 WebUI 插件配置里勾选"
        )

    def _sign_proxy_enabled(self) -> bool:
        """音乐卡片签名代理的开关（``profiles.onebot.enable_sign_proxy``）。

        **固定按 OneBot 读，不按事件**：签名代理是插件启动时起的常驻服务
        （那时还没有消息事件），而且它只服务 OneBot 协议端 ——
        官方机器人根本不发音乐卡片。选「通用」配置来源时自动读
        ``music.enableSignProxy``。
        """
        return bool(profile_value(
            self._platform_profiles(), "aiocqhttp", "enable_sign_proxy",
            self.conf_get, True,
        ))

    async def _start_sign_proxy(self) -> None:
        """按配置启动音乐卡片签名代理。

        关掉它只影响「音乐卡片」这一种发送方式（voice/file/link 都不依赖
        签名服务，因为那些不走 lightApp 卡片）。
        """
        if not self._sign_proxy_enabled():
            logger.info("[R插件] 音乐卡片签名代理已在配置里关闭")
            return
        port = int(self.conf_get("music.signProxyPort", 18888) or 18888)
        upstream = str(self.conf_get("music.signProxyUpstream", "") or "").strip()
        proxy = SignProxy(upstream or MUSIC_SIGN_UPSTREAM, port)
        if await proxy.start():
            self._sign_proxy = proxy

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

        先做 Cookie 逐项填写「dict -> template_list」的结构性迁移、再把散落的
        点歌配置搬进 ``music`` 分组，最后做通用类型自愈。顺序不能反：
        通用自愈会把 template_list 的旧 dict 值包成 ``[{...}]``，反而弄坏配置；
        而点歌配置的搬迁要赶在自愈「用默认值补齐新键」之前才有意义
        （详见 ``core/config_migrate.py::migrate_music_config``）。
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

        # 2) 点歌配置搬进 music 分组 + 清理废弃键
        music_changes = migrate_music_config(self.conf_data)

        # 3) 通用类型自愈（不在这里触发保存，等前面都处理完统一存一次）
        heal_changes = heal_config(self.conf_data, schema, save=None)

        changes = migrate_changes + music_changes + heal_changes
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
    # 平台能力（OneBot v11 / QQ 官方机器人 …）
    # ==================================================================

    def _platform_profiles(self) -> dict:
        """配置里的「分协议端配置」分组（``profiles``）。

        形态见 ``core/platform_profiles.py``::

            {"mode": "per_platform",
             "onebot": {...}, "qqofficial": {...}, "fallback": {...}}

        ``mode=per_platform``（默认）时按事件来自哪个协议端取对应那一组；
        ``mode=shared`` 时全部回到 ``plugin.*`` / ``music.*`` 里的旧值。

        ⚠️ **这个键必须存在 ``_conf_schema.json`` 里**：AstrBot 会在插件代码
        执行前按 schema 裁剪配置，schema 里没有的键读出来永远是空的
        （v1.6.7 的 ``plugin.platformProfiles`` 就是这么变成死代码的）。
        """
        raw = self.conf_get("profiles", None)
        return raw if isinstance(raw, dict) else {}

    def _caps_key(self, event: AstrMessageEvent | None) -> str:
        """当前事件的协议端 key（``aiocqhttp`` / ``qqofficial`` …）。"""
        if event is None:
            return ""
        try:
            return self._caps(event).key
        except Exception as exc:  # noqa: BLE001 - 探测失败按未知处理
            logger.debug(f"[R插件] 协议端探测失败: {exc}")
            return ""

    def conf_plat(self, event: AstrMessageEvent | None, field: str, default=None):
        """读「发送形态」配置 —— **按当前协议端各读各的**。

        为什么要有这一层：OneBot 能发合并转发和音乐卡片，QQ 官方机器人
        两样都没有，但能发原生 markdown。把两边的偏好混在一个配置里，
        官机用户看到「用聊天记录」这种选项只能一脸问号。

        取值的完整优先级见 ``core/platform_profiles.value()``：
        分协议端模式下读 ``profiles.<组>.<字段>``，缺失才落到该组默认值。

        Args:
            event: 当前消息事件。传 ``None``（后台任务、配置面板）时按
                「未知协议端」处理 —— 落在 ``fallback`` 组，取保守值。
            field: 字段名（见 ``core/platform_profiles.FIELDS``）。
        """
        return profile_value(
            self._platform_profiles(),
            self._caps_key(event),
            field,
            self.conf_get,
            default,
        )

    def _caps(self, event: AstrMessageEvent | None) -> PlatformCaps:
        """当前事件所属协议端的能力表。

        所有「能不能发合并转发 / 音乐卡片 / 原生 MD」的判断都走这里。
        QQ 官方机器人不支持前两者，配置里开着也不该走 —— 硬发会让
        **整条消息链失败**（用户看到的是「什么都没发出来」）。
        """
        try:
            return caps_for(event)
        except Exception as exc:  # noqa: BLE001 - 探测失败不能拖垮发送
            logger.debug(f"[R插件] 平台能力探测失败，按最保守能力处理: {exc}")
            return caps_for(None)

    def _forward_enabled(self, event: AstrMessageEvent | None = None) -> bool:
        """解析内容是否用「聊天记录（合并转发）」发送。

        由 ``profiles.<协议端>.send_as_forward`` 控制（选「通用」配置来源时
        读 ``plugin.send_as_forward``）；聊天里可用
        ``#R配置 形式 聊天记录`` / ``#R配置 形式 直发`` 随时切换。

        **但平台能力优先**：QQ 官方机器人（botpy）没有合并转发消息段，
        配置开着也会整条发送失败，所以这里强制回落到直发。
        ``event`` 为 None（配置面板展示等场景）时只看配置开关。
        """
        if event is not None and not self._caps(event).forward:
            return False
        return bool(self.conf_plat(event, "send_as_forward", False))

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
    # 入口一点五：分享卡片（QQ 群里分享的「小程序卡片」，不是链接文本）
    # ==================================================================

    @filter.custom_filter(BiliMiniappFilter)
    async def on_bili_miniapp(self, event: AstrMessageEvent):
        """分享卡片 → 提取跳转链接 → 按识别到的平台走解析。

        卡片是 ``CQ:json`` 消息段，``get_message_str()`` 是空串，正则匹配不到，
        所以用自定义 filter 检查消息组件链（见 ``BiliMiniappFilter``）。

        两类卡片都走这里：

        - **B 站小程序**：appid ``1109937557``，跳转链接在 ``qqdocurl``
          （``b23.tv`` 短链）。这条链路先于通用链走，因为它能兜底抠 BV 号。
        - **其它卡片**（小红书、微博…）：从卡片原文里抠出链接后交给规则表判断。

        不管能不能解析，卡片原文都会记进日志（见 ``_capture_card_links``），
        方便按真实字段补规则。
        """
        messages = event.get_messages()

        # 先把卡片原文记下来（只记录、不发送），排查字段用
        for comp in messages:
            if isinstance(comp, Comp.Json):
                _capture_card_links(event, comp)

        # ---- B 站小程序：仍然用原来的专用链路（有 BV 号兜底）----
        link = _find_bili_link_in_messages(messages)
        if link:
            logger.info(f"[R插件] 识别到 B 站小程序卡片: {link[:80]}")
            async for item in self._dispatch(
                event, link, forced_resolver="bilibili", forced_name="哔哩哔哩"
            ):
                yield item
            return

        # ---- 其它平台卡片：抠出链接，交给统一派发（内部会按规则表识别平台）----
        for comp in messages:
            if not isinstance(comp, Comp.Json):
                continue
            link = _find_card_link(comp)
            if not link:
                continue
            rule = match_rule(link, AUTO_RULES)
            name = rule.name if rule else ""
            logger.info(f"[R插件] 识别到 {name or '未知平台'} 卡片: {link[:100]}")
            async for item in self._dispatch(event, link):
                yield item
            return

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
    # 入口三：等 Cookie 时的「下一条消息」
    # ==================================================================

    @filter.custom_filter(CookieInputFilter)
    async def on_cookie_input(self, event: AstrMessageEvent):
        """把「等待 Cookie」状态下管理员发的下一条消息当成 Cookie 收下。

        两步式设置（``#R配置 cookie 小红书`` → 机器人提示 → 粘贴整串）比
        一条长命令好：Cookie 动辄几百上千字符，塞进命令里既容易截断，
        也会留在聊天记录里。

        **consume 必须在第一个 await 之前**：asyncio 是单线程，同一个会话的
        两条消息几乎同时进来时，如果先把状态删掉放在 await 之后，两条都会
        被当成 Cookie 各写一次（序号点播踩过同样的坑）。所以这里 peek 完
        立刻 consume。
        """
        umo = event.unified_msg_origin
        entry = self._cookie_pending.get(umo)
        if not entry:
            return

        # ---- 先消费掉等待状态，再往下走（下面就开始有 await 了）----
        self._cookie_pending.pop(umo, None)
        deadline, platform = entry

        label = self._cookie_label(platform)
        if time.time() > deadline:
            yield event.plain_result(f"⌛ 等待超时，请重新发「#R配置 cookie {label}」")
            event.stop_event()
            return

        text = event.get_message_str().strip()
        if text in ("取消", "cancel", "#取消", "/取消", "算了"):
            yield event.plain_result(f"已取消设置 {label} 的 Cookie。")
            event.stop_event()
            return

        yield event.plain_result(self._set_cookie(platform, text))
        event.stop_event()

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

        if self.conf_plat(event, "only_group", False) and event.is_private_chat():
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
                if not candidate:
                    logger.info(
                        f"[R插件] 链接未命中任何平台规则，跳过: {url[:100]}"
                    )
                    continue
                if candidate.key not in enabled:
                    # 这条日志很重要：用户反馈「某平台没触发」时，绝大多数情况
                    # 是这里被 enabled_platforms 过滤掉了。以前不打日志，排查时
                    # 日志里完全没有痕迹，只能去翻配置。
                    logger.info(
                        f"[R插件] {candidate.name} 未在 plugin.enabled_platforms "
                        f"里启用，跳过（可在 WebUI 插件配置里勾选）: {url[:80]}"
                    )
                    continue
                if candidate.name in blacklist:
                    logger.info(
                        f"[R插件] {candidate.name} 在全局黑名单里，跳过: {url[:80]}"
                    )
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
                    # 区分两类失败，日志也分开：
                    # - 按规则拒绝（rejected）：作品已解析出来，只是被配置限制/能力
                    #   约束拦住。这类原因用户自己能改，**必须**告诉他；
                    # - 真失败：网络抖动、接口报错、链接失效。默认静默，
                    #   免得群里每次网络超时都刷一条报错。
                    if result.rejected:
                        logger.warning(
                            f"[R插件] {result.platform} 按规则未发送: {result.error}"
                        )
                        # 既然作品信息已经拿到了，就把标题和链接一起发出去，
                        # 而不是只丢一句「超时长」让用户自己想办法。
                        async for item in self._render_text_only(event, result):
                            yield item
                    else:
                        logger.warning(f"[R插件] {result.platform} 解析失败: {result.error}")
                        if result.error and self.conf_plat(event, "reply_on_error", False):
                            yield event.plain_result(f"❌ {result.platform}：{result.error}")
                else:
                    # 拿到了信息但没有媒体（比如 B 站限流拿不到直链），把文字情报发出去
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
        """没有媒体、只有文字信息时的输出。

        两类场景都会走到这里：

        - **B 站限流拿不到直链**（``success=True`` 但无媒体）：把标题 / 作者
          这些情报发出去，总比什么都不发好；
        - **视频超过配置的时长上限**（``rejected``）：除了说明原因，还要给出
          **作品页链接** —— 只告诉用户「太长不发」，他知道为什么不发了，
          却不知道该去哪看。原版到这里就断了（只发文字、连链接都没有），
          想看只能自己拿标题去搜。
        """
        lines = [f"🔗 {result.platform}"]
        if result.title:
            lines.append(f"标题：{result.title}")
        if result.author:
            lines.append(f"作者：{result.author}")
        if result.error:
            lines.append(
                f"⏱️ {result.error}" if result.rejected else f"备注：{result.error}"
            )

        url = result.extra.get("web_url")
        if url:
            lines.append(f"👉 观看地址：{url}")

        yield event.plain_result("\n".join(lines))

    async def _render(self, event: AstrMessageEvent, result: ResolveResult):
        """把解析结果渲染成 AstrBot 消息。

        两种「发送形式」，由 ``plugin.send_as_forward`` 决定：

        - **直发**（默认，走 ``_render_direct``）：先发文字简介，再逐个发媒体
        - **聊天记录**（走 ``_render_forward``）：简介与全部媒体打包成一条合并转发

        两条路的顺序都是「先简介、后媒体」——用户要先看到这条作品是什么、
        谁发的，再看到内容本身，而不是先被媒体刷屏。

        评论（B站/抖音）是附加链路，不参与上面的打包：它们本来就是各自一条
        合并转发，所以统一放在最后补发，两条路都能走到。
        """
        async for item in self._render_body(event, result):
            yield item

        # ---- B站评论（附加功能，失败不拖垮主流程）----
        async for item in self._maybe_send_bili_comments(event, result):
            yield item

        # ---- 抖音评论（附加功能，失败不拖垮主流程）----
        async for item in self._maybe_send_douyin_comments(event, result):
            yield item

    async def _render_body(self, event: AstrMessageEvent, result: ResolveResult):
        """按配置选发送形式，并把「识别前缀 / 是否带简介」算出来。

        前缀是给``_build_intro``用的（简介文本里那一行），不是单独发出去的
        提示 —— 开了聊天记录转发之后，简介直接进第一个节点，不会再有一条
        独立的「识别成功」消息。
        """
        # 识别前缀沿用原 Guoba 面板配置；原版默认空串，这里给个更直观的兜底
        prefix = str(self.conf_get("global.identifyPrefix", "") or "").strip() or "🔗 识别："
        show_desc = bool(self.conf_plat(event, "show_desc", True))

        # ---- 纯文本类结果（AI 总结 / 翻译）----
        # 这类结果本身没有媒体，包成聊天记录只会多一层翻页，永远直发
        if result.extra.get("text_only") and result.desc:
            yield event.plain_result(f"{prefix}{result.platform}\n{result.desc}")
            return

        if self._forward_enabled(event):
            async for item in self._render_forward(event, result, prefix, show_desc):
                yield item
        else:
            async for item in self._render_direct(event, result, prefix, show_desc):
                yield item

    async def _render_direct(
        self,
        event: AstrMessageEvent,
        result: ResolveResult,
        prefix: str,
        show_desc: bool,
    ):
        """直发：简介单独一条，媒体逐条发出去。

        一个作品可能同时有多种媒体：抖音动图就是「多个视频 + BGM」，
        B 站合并产出的是本地视频。所以这里不是 if/elif 一路到底，
        而是依次追加。
        """
        # ---- 先发文字简介（类型 + 标题 + 作者）----
        #
        # 官机上改走 **markdown**：作品文案放代码框（标记写「标题」）——
        # 否则文案里的 `#话题` 每一行都会被渲染成大标题。
        # 别的协议端保持纯文本：那条路没有 markdown，`#` 本来就不会被解析。
        #
        # ⚠️ 但**图集 / 多图要跳过** —— 那条路自己会发一条带标题的 MD
        # （``_gallery_md_text``），不跳会重复两条标题。
        if show_desc and not self._will_send_gallery_md(event, result):
            intro = self._build_intro(result, prefix)
            if intro:
                md = (
                    self._build_intro_md(result)
                    if self._caps(event).markdown
                    else None
                )
                if md:
                    chain = event.chain_result([Comp.Plain(md)])
                    if hasattr(chain, "use_markdown"):
                        chain.use_markdown(True)
                    yield chain
                else:
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
                comp = await self._video_component(merged, event)
                if comp is None:
                    raise MergeError(f"合并产物不可读: {merged}")
                yield event.chain_result([comp])
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
                comp = await self._video_component(path, event)
                if comp is None:
                    raise RuntimeError(f"视频文件不可读: {path}")
                yield event.chain_result([comp])
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
            send_mode = self.conf_plat(event, "send_mode", "url")
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
                    comp = await self._video_component(local_path, event)
                    if comp is None:
                        raise RuntimeError(f"视频文件不可读: {local_path}")
                    yield event.chain_result([comp])
                    sent_media = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[R插件] 发送下载后的视频失败: {exc}")

            if not sent_media:
                chain = self._build_video_chain(event, result)
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

    async def _render_forward(
        self,
        event: AstrMessageEvent,
        result: ResolveResult,
        prefix: str,
        show_desc: bool,
    ):
        """聊天记录形式：把整条解析结果打包成**一条合并转发**发出去。

        节点构成（顺序固定）：

        1. 简介节点（类型 / 标题 / 作者）—— ``show_desc`` 关掉时没有这个节点
        2. 之后每个媒体各占一个节点，顺序与作品本身一致

        **不再发**「🔗 识别：xx」那条独立提示 —— 简介已经进了第一个节点，
        单独再发一条纯属噪音。

        媒体遵守既有铁律：能落盘的一律先落盘、再转 base64 塞进节点
        （``Comp.Image.fromFileSystem`` / ``_video_component``），和直发走的是
        同一条跨容器安全路径。

        没有任何媒体时**不硬凑聊天记录**：一条只有文字的转发，点开跟普通
        消息看到的一模一样还多一次点击，这种情况退回普通文本情报。
        """
        node_name = (event.get_sender_name() or "").strip() or "解析结果"
        node_uin = str(event.get_sender_id() or "")

        def _node(comps: list) -> Comp.Node:
            # 一个节点一个媒体：和 _send_album 的转发分支保持一致，
            # 这样在 QQ 客户端里每个媒体都是独立一条「消息」，可单独转发/保存
            return Comp.Node(comps, name=node_name, uin=node_uin)

        nodes: list = []
        if show_desc:
            intro = self._build_intro(result, prefix)
            if intro:
                nodes.append(_node([Comp.Plain(intro)]))
        intro_nodes = len(nodes)

        failures: list[str] = []

        # ---- B站 DASH 延迟合并 ----
        skip_images = False
        skip_videos = False
        dash_merge = result.extra.get("dash_merge")
        if dash_merge and dash_merge.get("video") and dash_merge.get("audio"):
            comp = None
            try:
                merged = await merge_dash(
                    dash_merge["video"],
                    dash_merge["audio"],
                    tag=f"bili_{result.extra.get('bvid', 'x')}",
                )
                event.track_temporary_local_file(str(merged))
                comp = await self._video_component(merged, event)
                if comp is None:
                    raise MergeError(f"合并产物不可读: {merged}")
            except MergeError as exc:
                # 合并失败降级为无声视频轨（和直发同策略）
                logger.warning(f"[R插件][B站] 合并失败，降级为无声视频轨: {exc}")
                try:
                    comp = Comp.Video.fromURL(dash_merge["video"])
                except Exception as exc2:  # noqa: BLE001
                    logger.warning(f"[R插件][B站] 无声视频轨也发送失败: {exc2}")
                    comp = None
                    failures.append(f"B站视频（{exc}）")
            if comp is not None:
                nodes.append(_node([comp]))
                skip_images = True  # 视频已进节点，封面图不再单独占一个节点

        # ---- 本地视频（其它平台的合并产物）----
        for path in result.local_videos[:1]:
            event.track_temporary_local_file(path)
            comp = await self._video_component(path, event)
            if comp is None:
                failures.append(f"本地视频（{Path(path).name} 不可读）")
                continue
            nodes.append(_node([comp]))

        # ---- 图集（抖音：静态图与动图混排，顺序按作品原样）----
        failed_album = 0
        if result.extra.get("album_kinds"):
            kinds = result.extra.get("album_kinds") or []
            still_paths = await self._download_album_stills(event, result, list(result.images))
            anim_paths = await self._download_album_videos(result, list(result.videos))

            vi = 0
            ii = 0
            for kind in kinds:
                if kind == "animated":
                    if vi >= len(anim_paths):
                        continue
                    path = anim_paths[vi]
                    vi += 1
                    if path is None:
                        failed_album += 1
                        continue
                    event.track_temporary_local_file(str(path))
                    comp = await self._video_component(path, event)
                    if comp is None:
                        failed_album += 1
                        continue
                    nodes.append(_node([comp]))
                else:
                    if ii >= len(still_paths):
                        continue
                    path = still_paths[ii]
                    ii += 1
                    if path is None:
                        failed_album += 1
                        continue
                    event.track_temporary_local_file(str(path))
                    try:
                        nodes.append(_node([Comp.Image.fromFileSystem(str(path))]))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[R插件] 转发节点图片构造失败: {exc}")
                        failed_album += 1
            skip_images = True
            skip_videos = True

        # ---- 视频直链 ----
        if result.videos and not skip_videos:
            send_mode = self.conf_plat(event, "send_mode", "url")
            added = False
            oversize = False
            if send_mode == "download":
                local_path, oversize_msg = await self._download_video(result)
                if oversize_msg:
                    # 超限是明确结论：这个视频直接放弃，不退回直链
                    # （直发路径也是这么判的，否则等于绕过了大小限制）
                    oversize = True
                    failures.append(str(oversize_msg).removeprefix("⚠️ ").strip())
                elif local_path:
                    event.track_temporary_local_file(local_path)
                    comp = await self._video_component(local_path, event)
                    if comp is None:
                        failures.append("下载后的视频文件不可读")
                    else:
                        nodes.append(_node([comp]))
                        added = True

            if not added and not oversize:
                # 直链模式，或下载失败——退回把解析出的地址交给发送端（同直发）
                for comp in self._build_video_chain(event, result):
                    nodes.append(_node([comp]))
                    added = True
                if not added:
                    failures.append("视频链接不可用")

        # ---- 音频（音乐平台结果 / 抖音背景音乐）----
        #
        # 这里**必须先落盘再进节点**，不能像直发那样直接把 URL 交给
        # ``Comp.Record.fromURL``：合并转发里 Record 由 AstrBot 自己下载并转成
        # wav（``Node.to_dict`` → ``Record.convert_to_base64``），那一步失败会抛
        # 异常，把**整条聊天记录**一起拖垮 —— 直发时它只是单独一条消息，转发时
        # 它是同一个消息链的一部分，代价完全不同。
        audio_max = int(self.conf_get("global.videoSizeLimit", 70) or 70) * 1024 * 1024
        for url in result.audios[:3]:
            try:
                path = await download_media(url, prefix="audio", max_bytes=audio_max)
            except Exception as exc:  # noqa: BLE001 - 单个音频失败不拖垮整条转发
                logger.warning(f"[R插件] 转发节点音频下载失败，跳过: {exc}")
                failures.append("音频")
                continue
            event.track_temporary_local_file(str(path))
            try:
                nodes.append(_node([Comp.Record.fromFileSystem(str(path))]))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 转发节点音频构造失败: {exc}")
                failures.append("音频")

        # ---- 图片 ----
        failed_images = 0
        if result.images and not skip_images and not skip_videos:
            paths = await self._download_images(event, result, list(result.images))
            for path in paths:
                if path is None:
                    failed_images += 1
                    continue
                event.track_temporary_local_file(str(path))
                try:
                    nodes.append(_node([Comp.Image.fromFileSystem(str(path))]))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[R插件] 转发节点图片构造失败: {exc}")
                    failed_images += 1

        if failed_album:
            failures.append(f"{failed_album} 项图集媒体")
        if failed_images:
            failures.append(f"{failed_images} 张图片")

        media_nodes = len(nodes) - intro_nodes
        if media_nodes <= 0:
            # 一个媒体都没能进节点：不硬发空壳聊天记录，退回文字情报
            logger.warning(f"[R插件] {result.platform} 无媒体可转发，退回文本")
            async for item in self._render_text_only(event, result):
                yield item
            return

        if failures:
            detail = "；".join(dict.fromkeys(failures))
            yield event.plain_result(f"⚠️ 部分内容未能发送：{detail}")

        logger.info(
            f"[R插件] 以聊天记录发送 {result.platform}：{media_nodes} 个内容节点"
            + ("（含简介节点）" if intro_nodes else "")
        )
        yield event.chain_result([Comp.Nodes(nodes)])

    # ---- 评论图片 ----
    # 每条评论最多带几张图：带图评论常见 1 张，九宫格也有，3 张够用且不至于刷屏
    _COMMENT_IMAGE_MAX = 3
    # 单张评论图大小上限。抖音评论原图是 1600×1600（约 660 KB），8MB 很宽裕；
    # 卡个上限是防止「10 条评论 × 多图」把临时目录撑爆
    _COMMENT_IMAGE_MAX_BYTES = 8 * 1024 * 1024

    def _comment_md_text(self, comments: list[dict], label: str) -> str:
        """（官机）把评论拼成**一条 markdown**：昵称 / 正文 / 时间都在代码框里。

        **为什么官机走这条**：

        1. 官机**没有合并转发**（``Comp.Nodes`` 那个消息段适配器根本不认），
           原来的评论路径在官机上等于发不出去；
        2. 评论正文里什么都有 —— ``#话题`` / ``*强调*`` / ``~~删除~~`` 都会
           被 markdown 解析，排版立刻垮掉，所以**整条包进代码框**（框内原样）；
        3. **图片和动图都不带** —— 官机的媒体通道一次只能传一个，
           几条评论就能把消息刷满屏。用户明确要这个形态。

        代码框的**语言标记位置放评论人昵称** —— 客户端会把它渲染成框的标题，
        比在正文里再写一遍 ``【昵称】`` 更省版面（用户指定）。
        昵称里的空白和反引号要清掉，否则语言标记解析不了；正文里的 `` ``` ``
        也要替换（不然框会被提前闭合）。
        """
        blocks: list[str] = []
        for c in comments:
            name = str(c.get("nickname") or "").strip()
            text = str(c.get("text") or "").strip()
            if not (name or text):
                continue
            tag = re.sub(r"[\s`]+", "", name)[:20]
            safe = text.replace("```", "'''")
            blocks.append(f"```{tag}\n{safe}\n```")
        if not blocks:
            return ""
        return "\n\n".join([f"# 💬 {label} · 评论（{len(blocks)} 条）", *blocks])

    async def _build_comment_nodes(
        self, event: AstrMessageEvent, comments: list[dict]
    ) -> list:
        """把评论列表转成合并转发节点，**带图的先把图落到本地**。

        排布规则（对齐用户要的观感）：

        - **静态图**：文字在上、图片在下，放**同一个节点**（一条评论 = 一条记录）；
        - **动图**：转成 mp4 后**单独成一条**（文字另起一条）—— 实测动图和静态图
          混在同一节点里，QQ 那边的渲染顺序会乱掉。

        为什么要落盘：抖音评论图直链是带签名的 CDN 地址，需要正确的 Referer、
        而且同一张图有 4 个 CDN 候选 —— AstrBot 发送端的下载器两者都不具备，
        直发会**整条消息链一起失败**（详见 ``_download_album_stills`` 的说明）。

        昵称用评论者、QQ 号用发起解析的用户（对齐合并转发的身份规则）。
        """
        sender_uin = str(event.get_sender_id() or "")
        nodes: list = []

        for c in comments:
            text = str(c.get("text") or "")
            nickname = c["nickname"]
            statics: list = []
            videos: list = []

            for cands in (c.get("images") or [])[: self._COMMENT_IMAGE_MAX]:
                try:
                    path = await download_media_candidates(
                        list(cands),
                        prefix="comment_img",
                        max_bytes=self._COMMENT_IMAGE_MAX_BYTES,
                    )
                except Exception as exc:  # noqa: BLE001 - 单张图失败不影响这条评论
                    logger.debug(f"[R插件][评论] 图片下载失败，跳过这张: {exc}")
                    continue
                event.track_temporary_local_file(str(path))

                # 动图在接口层没有任何标记（image_list 的字段和静态图一模一样），
                # 只能下下来看文件本身是不是动的
                comp_video = None
                if is_animated_image(path):
                    mp4 = await animated_to_mp4(path)
                    if mp4:
                        event.track_temporary_local_file(str(mp4))
                        try:
                            comp_video = await self._video_component(mp4, event)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(f"[R插件][评论] 动图转组件失败: {exc}")
                if comp_video is not None:
                    videos.append(comp_video)
                    continue

                # 静态图（或动图转码失败时的降级）
                try:
                    statics.append(Comp.Image.fromFileSystem(str(path)))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件][评论] 图片组件构造失败: {exc}")

            if videos:
                # 有动图：文字一条、每张动图各一条，静态图再跟在后面
                if text:
                    nodes.append(
                        Comp.Node([Comp.Plain(text)], name=nickname, uin=sender_uin)
                    )
                for comp in videos:
                    nodes.append(Comp.Node([comp], name=nickname, uin=sender_uin))
                if statics:
                    nodes.append(Comp.Node(statics, name=nickname, uin=sender_uin))
            else:
                comps = ([Comp.Plain(text)] if text else []) + statics
                # 纯图评论 text 为空，但 comps 里有图 —— 不能因为没文字就丢掉
                if comps:
                    nodes.append(Comp.Node(comps, name=nickname, uin=sender_uin))

        return nodes

    async def _maybe_send_bili_comments(
        self, event: AstrMessageEvent, result: ResolveResult
    ):
        """B 站视频解析成功后，按配置抓评论并用合并转发发出来。

        评论是附加功能：平台不是 B 站、开关没开、没有 aid、抓不到，都直接
        跳过，绝不影响主流程。发出去的形态用原版截图失败时的兜底方案——
        文本（+ 图片）合并转发，不依赖截图。
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

        # ---- 官机：没有合并转发，改发「一条 MD + 代码框」（不带图/动图）----
        if self._caps(event).markdown:
            md = self._comment_md_text(comments, "哔哩哔哩")
            if md:
                chain = event.chain_result([Comp.Plain(md)])
                if hasattr(chain, "use_markdown"):
                    chain.use_markdown(True)
                yield chain
            return

        nodes = await self._build_comment_nodes(event, comments)
        if not nodes:
            return
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

        # ---- 官机：没有合并转发，改发「一条 MD + 代码框」（不带图/动图）----
        if self._caps(event).markdown:
            md = self._comment_md_text(comments, "抖音")
            if md:
                chain = event.chain_result([Comp.Plain(md)])
                if hasattr(chain, "use_markdown"):
                    chain.use_markdown(True)
                yield chain
            return

        nodes = await self._build_comment_nodes(event, comments)
        if not nodes:
            return
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

    def _build_intro_md(self, result: ResolveResult) -> str | None:
        """（官机）简介的 **markdown 版** —— 作品文案放进代码框，标记写「标题」。

        为什么文案必须进代码框：``#`` 是 markdown 的**标题语法**，而作品文案
        （尤其抖音）几乎全是 ``#话题`` 标签、还常分多行 —— 一旦当正文，
        **每一行都会变成大标题**，整片排版垮掉（v1.6.10 实测踩过）。

        代码框的**语言标记位置写「标题」** —— 客户端会把它渲染成框的标题，
        这样一眼能看出框里是作品文案。

        纯文本路径（``_build_intro``）**保持不动** —— 那条路没有 markdown，
        ``#`` 本来就不会被解析，改了反而多余。

        :return: markdown 文本；类型/标题/作者全空时返回 ``None``（调用方退回纯文本）。
        """
        ctype = self._content_type(result)
        if not (ctype or result.title or result.author):
            return None

        lines = [f"# {result.platform}" + (f" · {ctype}" if ctype else ""), ""]
        if result.title:
            safe = result.title.replace("```", "'''")
            lines += [f"```标题\n{safe}\n```", ""]
        if result.author:
            lines += [f"> {result.author}", ""]
        return "\n".join(lines).rstrip()

    def _will_send_gallery_md(
        self, event: AstrMessageEvent, result: ResolveResult
    ) -> bool:
        """官机上这条结果会不会**自己发一条带标题的 MD**（图集 / 多图）。

        用来避免「简介 + 图集 MD」两条都带标题。判断条件必须和
        ``_send_album`` / ``_send_images`` 的官机分支保持一致：

        * ``album_kinds`` 存在（抖音图集）→ ``_send_album`` 会发带标题的 MD；
        * 图片多于一张 → ``_send_images`` 会走 MD 分支（同样带标题）。

        单张图**不算** —— 那张走逐条发送，没有标题，简介还是要发的。
        """
        if not self._caps(event).markdown:
            return False
        if result.extra.get("album_kinds"):
            return True
        return len(result.images) > 1

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

    async def _video_component(
        self, path: str | Path, event: AstrMessageEvent | None = None
    ):
        """把一个本地视频文件变成可发送的 ``Comp.Video``。

        **两种协议端的要求是相反的**，所以这里按平台分支：

        +----------------+---------------------------+--------------------------+
        | 协议端         | 组件形态                  | 原因                     |
        +================+===========================+==========================+
        | **官机**       | ``fromFileSystem(path)``  | 适配器要**真实本地路径** |
        | (qq_official)  |                           | 它自己读文件传腾讯       |
        +----------------+---------------------------+--------------------------+
        | NapCat 等      | ``fromBase64(...)``       | 协议端在**另一个容器**， |
        | (aiocqhttp)    |                           | 读不到这边的路径         |
        +----------------+---------------------------+--------------------------+

        **官机为什么不能给 base64**（2026-09-24 线上事故）：AstrBot 的
        ``_parse_to_qqofficial`` 对 Video 只做::

            if is_file_uri(i.file):
                video_file_source = file_uri_to_path(i.file)   # file:///tmp/x.mp4 -> /tmp/x.mp4
            else:
                video_file_source = i.file                      # base64://... 原样

        然后 ``upload_group_and_c2c_media`` 开头就 ``Path(file_source).is_file()`` ——
        拿 ``base64://AAAA…`` 当文件名去 stat，直接::

            OSError: [Errno 36] File name too long: 'base64:/AAAAIGZ0eXB…'

        （``Path()`` 会把 ``//`` 折成 ``/``，所以日志里只剩一个斜杠。）
        这个异常抛到 pipeline 的 send 阶段，**整条消息发不出去** ——
        用户看到的就是「机器人没反应」。

        而 ``fromFileSystem`` 恰好被适配器认：``is_file_uri`` 为真 → 转回真实路径
        → ``os.path.exists`` 为真 → 读文件转 base64 → 传给腾讯。**所以官机反而
        只能用 fromFileSystem**（NapCat 那边禁用它的理由在这里不成立：官机是
        AstrBot 自己读文件走 HTTP 上传，不依赖协议端读盘）。

        **为什么是 async**：base64 那条路要读文件 + 编码，都是**同步阻塞**的。
        一个 70MB 的视频要先整个读进内存再编码，峰值内存约 100MB、耗时数百毫秒。
        直接在事件循环里做，这期间 AstrBot 所有协程都被卡住（包括其它会话的
        消息处理），所以丢到线程池执行。官机那条路只是拼个字符串，不用线程。

        :param event: 用来判断协议端；给 ``None`` 时按最保守的 base64 走。

        Returns:
            可发送的 ``Comp.Video``；文件不存在或读取失败时返回 ``None``。
        """
        p = Path(path)
        if not p.is_file():
            logger.warning(f"[R插件] 视频文件不存在，无法发送: {p}")
            return None

        # ---- 官机：适配器要真实本地路径（给 base64 会让它 Path().is_file() 炸）----
        if self._is_qq_official(event):
            return Comp.Video.fromFileSystem(str(p))

        try:
            data = await asyncio.to_thread(_read_video_base64, p)
        except OSError as exc:
            logger.warning(f"[R插件] 读取视频失败 {p}: {exc}")
            return None

        if not data:
            return None
        return Comp.Video.fromBase64(data)

    def _is_qq_official(self, event: AstrMessageEvent | None) -> bool:
        """当前事件是不是 QQ 官方机器人（``qq_official``）。"""
        if event is None:
            return False
        try:
            return self._caps(event).key == "qqofficial"
        except Exception:  # noqa: BLE001
            return False

    async def _download_album_stills(
        self, event: AstrMessageEvent, result: ResolveResult, still_images: list[str]
    ) -> list[Path | None]:
        """下载图集里的静态图，返回与 ``still_images`` 等长的路径列表。

        **为什么必须自己下载，不能把 URL 直接交给发送端？**

        抖音图集的图片直链是 ``p3-pc-sign.douyinpic.com`` 这类**带签名的 CDN
        地址**，而且：

        1. 同一个 ``url_list`` 里有 ``.webp``（压缩预览）和 ``.jpeg``（原图）
           两个变体，**体积差一倍以上**；403 出现在哪个位置是**每张图固定的**，
           不是随机的（实测同一张图 3 轮探测结果一致）。所以「按原顺序试到
           第一个成功就用」会**大概率拿到模糊的 .webp**——排序由
           ``core.douyin_ssr.rank_image_candidates`` 负责改成原图优先；
        2. 下载需要带 ``Referer: https://www.douyin.com/``，否则大概率 403；
        3. 失败不是 4xx 而是 **200 + text/html 的 238 字节错误页**，
           由 ``_stream_one(expect_media=True)`` 拦掉。

        AstrBot 的 ``respond.stage`` 在真正发出 ``Comp.Image.fromURL(url)`` 时，
        是用它自己的下载器抓这个 URL 的——**不带 Referer、没有候选回退、
        也不校验内容**，只要那一张失败就抛 ``DownloadFileHTTPError``，
        **整条消息链一起失败**。这就是「图集一条都没发出来、只剩简介文字」的根因。

        所以图集的静态图一律先用本插件自己的下载器（``_headers_for`` 补
        Referer + 原图优先排序 + ``download_many_candidates`` 逐个候选回退）落到
        本地，发送端只发本地文件，不再碰远程签名 URL。
        """
        if not still_images:
            return []

        concurrency = max(
            1, int(self.conf_plat(event, "download_concurrency", 8) or 8)
        )
        max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
        max_bytes = max_mb * 1024 * 1024

        candidates = result.extra.get("image_candidates")
        if (
            isinstance(candidates, list)
            and len(candidates) == len(still_images)
            and all(isinstance(c, list) and c for c in candidates)
        ):
            paths = await download_many_candidates(
                candidates,
                prefix="album",
                max_bytes=max_bytes,
                concurrency=concurrency,
            )
        else:
            # 没有候选信息（旧结果 / 缓存里的老格式）就按单 URL 下载
            paths = await download_many(
                still_images,
                prefix="album",
                max_bytes=max_bytes,
                concurrency=concurrency,
            )
        return list(paths)

    async def _download_album_videos(
        self, result: ResolveResult, anim_videos: list[str]
    ) -> list[Path | None]:
        """下载图集里的动图视频，返回与 ``anim_videos`` 等长的路径列表。

        动图的播放直链（``aweme/v1/play``）同样会 403，而且 ``Comp.Video.fromURL``
        走的是发送端的下载器，失败同样会拖垮整条消息链。所以也一并落到本地。

        **逐个候选回退**：动图视频轨的 url_list 和静态图一样有多个候选，
        实测第一个常 403。``extra["animated_video_candidates"]`` 存了每个动图
        的全部候选，按顺序试到能下为止；没有候选信息（旧缓存）就退回单 URL。

        这里刻意用**串行**：动图视频体积大，并发下载容易把带宽打满、
        也让适配器那边的发送排队。数量通常个位数，串行完全可接受。
        """
        if not anim_videos:
            return []

        max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
        max_bytes = max_mb * 1024 * 1024

        raw_cands = result.extra.get("animated_video_candidates")
        cands_list: list[list[str]] | None = None
        if (
            isinstance(raw_cands, list)
            and len(raw_cands) == len(anim_videos)
            and all(isinstance(c, list) and c for c in raw_cands)
        ):
            cands_list = raw_cands

        paths: list[Path | None] = []
        for i, url in enumerate(anim_videos):
            urls = cands_list[i] if cands_list else [url]
            try:
                path = await download_media_candidates(
                    urls, prefix="album_video", max_bytes=max_bytes
                )
                paths.append(path)
            except Exception as exc:  # noqa: BLE001 - 单个动图失败不影响整批
                logger.warning(
                    f"[R插件][抖音] 动图下载失败（{len(urls)} 个候选）{url[:70]}: {exc}"
                )
                paths.append(None)
        return paths

    async def _host_images(self, urls: list[str]) -> list[str] | None:
        """把一组图片 URL **全部转存到国内 OSS**，返回等长直链；有一张失败返回 ``None``。

        **为什么官机必须走这一步**（2026-09-24）：官机 markdown 的图是
        **腾讯服务器下载转存**的（官方文档原话），而解析出来的原始直链常常
        取不到 —— 抖音 ``p3-sign.douyinpic.com`` 要 ``Referer``（本机实测恒定
        403）、各家 CDN 也未必对腾讯的下载器友好。

        统一过一遍 ``transfer_url``（czoss，落在广州电信）之后，交给腾讯的是一个
        **国内 + 内容固定 + https** 的地址，才有把握渲染出来。这也正是菜单图
        实测出来的路子（详见 ``core.image_bed``）。

        **并发**：9 张串行要 40 秒，``asyncio.gather`` 之后 5 秒左右。

        :return: 与 ``urls`` 等长的直链列表；任意一张失败就返回 ``None`` ——
            调用方退回逐条发送，不发一条半残（几张 ``[alt]``）的 MD。
        """
        key = self._czoss_key()
        results = await asyncio.gather(
            *[transfer_url(u, key=key) for u in urls], return_exceptions=True
        )
        out: list[str] = []
        for idx, item in enumerate(results):
            if not isinstance(item, str) or not item:
                logger.debug(
                    f"[R插件][图集] 第 {idx + 1}/{len(urls)} 张转存失败，放弃拼 MD"
                )
                return None
            out.append(item)
        logger.debug(f"[R插件][图集] {len(out)} 张已转存国内 OSS")
        return out


    async def _gallery_md_text(
        self,
        event: AstrMessageEvent,
        result: ResolveResult,
        urls: list[str],
        *,
        sizes: list | None = None,
        probe_missing: bool = False,
        limit: int = _MD_IMAGE_TEST_MAX,
        head: bool = True,
    ) -> str | None:
        """（官机）把一组图片拼成**一条 markdown 内嵌多图**；不满足条件返回 ``None``。

        为什么官机要走这条：**它没有合并转发**（``Comp.Nodes`` 适配器不认），
        所以 ``_send_images`` 一旦遇到 ``total > max_images`` 就只能「只发前 N 张」
        —— 用户实测过「三张图的图集只出来一张」（他的 ``plugin.max_images`` 是 1）。
        而 markdown 内嵌图**不看那个阈值**，能一条把图全发出来。

        尺寸（**硬要求**，不带尺寸时手机 QQ 只渲染 ``[alt]``）按优先级取：

        1. ``sizes`` 参数 / ``result.extra["image_sizes"]`` —— 抖音 ``images[i]``
           顶层自带 width/height，**零额外请求**；
        2. ``probe_missing=True`` 时对缺的那几张并发 ``_probe_image_size``
           （Range 只取 64KB 读 header，不下载整图）—— 快手这类第三方接口
           不返回尺寸，只能探。

        **任意一张取不到尺寸就整体放弃** —— 宁可退回逐条发送，也不要发一条
        手机上全是 ``[alt]`` 的消息（v1.6.7 实测踩过）。
        """
        if not urls or len(urls) > limit:
            return None

        # ---- ① **先全部转存到国内 OSS** ----
        # 官机 markdown 的图是**腾讯服务器**去下载的，原始直链（抖音签名 CDN 等）
        # 经常取不到 → 客户端只会给你「图片加载失败」。转存后是国内 + 内容固定的
        # https 地址，才有把握渲染。任意一张失败就整体放弃，退回逐条发送。
        hosted = await self._host_images(urls)
        if not hosted:
            return None
        urls = hosted

        known: list = list(
            sizes if sizes is not None else (result.extra.get("image_sizes") or [])
        )
        if len(known) < len(urls):
            known += [None] * (len(urls) - len(known))

        need = [i for i, s in enumerate(known) if not s]
        if need and probe_missing:
            probed = await asyncio.gather(
                *[self._probe_image_size(urls[i]) for i in need]
            )
            for i, size in zip(need, probed):
                known[i] = size

        # ---- ② 排版：一行放 2~3 张（用户实测这个最合适，不刷屏）----
        # 单图仍用单图宽度；多图换更小的尺寸，一行三张再等比缩一档。
        n_img = len(urls)
        if n_img <= 3:
            per_row = n_img          # 1/2/3 张都排一行
        elif n_img == 4:
            per_row = 2              # 2+2 比 3+1 顺眼
        else:
            per_row = 3              # 其余按三张一行铺
        if n_img > 1:
            base = self._md_multi_image_width(event)
        else:
            base = self._md_image_width(event)
        max_width = base if per_row <= 2 else max(80, base * 2 // 3)

        blocks: list[str] = []
        for i, url in enumerate(urls, 1):
            size = known[i - 1] if i - 1 < len(known) else None
            if not size:
                return None
            blocks.append(self._md_image(url, size, alt=f"图{i}", max_width=max_width))

        # ⚠️ **标题只用固定文字，绝不把作品文案拼进来**。
        #
        # markdown 的 `#` 是标题语法，而作品文案（尤其抖音）几乎全是
        # `#话题` 标签、还常分多行 —— 一旦塞进正文，**每一行都会变成标题**
        # （`# 一直说说说。#小猫 #罗小黑` 这种），整片排版垮掉。
        # 所以文案单独放，且用**代码框**包住（框内原样显示、不参与解析）。
        #
        # ``head=False`` 用于「图片太多、分批发」时的后续几条（5 张以上）——
        # 那时不重复标题和文案，免得刷屏。
        lines: list[str] = []
        if head:
            ctype = self._content_type(result) or "图片"
            lines = [f"# {result.platform} · {ctype}", ""]
            if result.author:
                lines += [f"> {result.author}", ""]
            if result.title:
                # 文案里若有 ``` 会把代码框提前闭合。
                # 语言标记位置写「标题」—— 客户端会把它渲染成框的标题
                # （和评论那边用昵称做标记是同一个形态）。
                safe_title = result.title.replace("```", "'''")
                lines += [f"```标题\n{safe_title}\n```", ""]

        # ⚠️ 排版规则（都实测过）：
        #
        # * **一行内用空格分隔** —— 一行两三张能横排（用户对比过：每行一张最刷屏，
        #   一行两张/三张最合适）；
        # * **行与行之间必须空行** —— 官方「换多行」说单换行不产生换行效果，
        #   紧贴的多行 `![…]` 会被当成同一段文本，手机只渲染第一张。
        lines.append(
            "\n\n".join(
                " ".join(blocks[i:i + per_row]) for i in range(0, len(blocks), per_row)
            )
        )
        return "\n".join(lines).rstrip()

    async def _send_album(self, event: AstrMessageEvent, result: ResolveResult):
        """发送抖音图集 —— **静态图当图片发，动图当视频发，顺序按作品原样**。

        原版 ``processDouyinImageAlbum`` 就是这么做的：逐项看有没有视频轨，
        有就下载下来当视频发（还会用 ffmpeg 合 BGM），没有就 ``segment.image``。

        **移植版的差异（重要）：** 原版在 Yunzai 下可以 ``segment.image(url)``
        把远程 URL 直接交出去，因为那边有统一的图片代理会补 Referer。AstrBot
        这边发送端的下载器不补 Referer、也没有候选回退，抖音的签名 CDN 直链
        会 403 并把整条消息链拖失败（详见 ``_download_album_stills`` 的说明）。
        所以这里**静态图和动图都先落到本地**，再发本地文件。

        发送规则：

        - 项数不超过 ``max_images``：一条消息链按顺序发完
        - 超过阈值：静态图/动图都下载到本地后，用合并转发完整发出（顺序不变）
        """
        limit = max(1, int(self.conf_plat(event, "max_images", 9) or 9))
        kinds = result.extra.get("album_kinds") or []
        n_still = kinds.count("still")
        n_anim = kinds.count("animated")

        # 动图视频链：videos 里全是动图，顺序与作品一致
        anim_videos = list(result.videos)
        still_images = list(result.images)

        if not kinds:
            return

        logger.debug(
            f"[R插件][抖音] 发送图集：静态图 {n_still} 张，动图 {n_anim} 个，"
            f"发送模式={'合并转发' if len(kinds) > limit else '直发'}"
        )

        # ---- 官机专属：一条 **markdown 内嵌多图** ----
        #
        # 为什么官机走这条，而不是「下载到本地再逐条发」：
        #
        # 1. markdown 内嵌图是**腾讯服务器**去下载的 —— 我们完全不用下，
        #    于是**绕开了本机对抖音图的 403**。实测同一批图在本机恒定 403
        #    （去 ~tplv 后缀 / 换 p3→p9→p6 节点 / 加 Referer / 带 1449 字符
        #    的 Cookie，全是 403），但交给腾讯去取就有机会成功；
        # 2. 官机没有合并转发，N 张图逐条发等于刷屏；
        # 3. 用户明确要这个形态。
        #
        # **只处理纯静态图集** —— markdown 里塞不进视频，含动图的仍走原路径。
        if self._caps(event).markdown and all(k == "still" for k in kinds):
            md = await self._gallery_md_text(event, result, list(result.images))
            if md:
                chain = event.chain_result([Comp.Plain(md)])
                if hasattr(chain, "use_markdown"):
                    chain.use_markdown(True)
                yield chain
                return
            logger.info(
                "[R插件][抖音] 图集缺尺寸，退回逐条发送"
                "（markdown 内嵌图不带尺寸时手机端只显示 [alt]）"
            )

        # ---- 分两路下载（静态图并发 + 带候选回退，动图串行）----
        still_paths = await self._download_album_stills(event, result, still_images)
        anim_paths = await self._download_album_videos(result, anim_videos)

        # 下载成功的才登记给 AstrBot 回收
        for path in [*still_paths, *anim_paths]:
            if path is not None:
                event.track_temporary_local_file(str(path))

        # ---- 第一档：直发（项数不超过阈值）----
        if len(kinds) <= limit:
            chain = []
            vi = 0
            ii = 0
            for kind in kinds:
                if kind == "animated":
                    if vi < len(anim_paths):
                        path = anim_paths[vi]
                        if path is not None:
                            comp = await self._video_component(path, event)
                            if comp is not None:
                                chain.append(comp)
                            else:
                                logger.warning(f"[R插件] 本地动图不可读: {path}")
                        vi += 1
                else:
                    if ii < len(still_paths):
                        path = still_paths[ii]
                        if path is not None:
                            try:
                                chain.append(Comp.Image.fromFileSystem(str(path)))
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(f"[R插件] 本地图片构造失败: {exc}")
                        ii += 1
            if chain:
                yield event.chain_result(chain)
            else:
                # 一张都没下下来 —— 明确告诉用户，别静默失败
                logger.warning(
                    f"[R插件][抖音] 图集媒体全部下载失败（共 {len(kinds)} 项）"
                )
                yield event.plain_result(
                    f"⚠️ 抖音图集媒体下载失败（共 {len(kinds)} 项），"
                    "可能是链接签名过期或 Cookie 失效，可稍后重试"
                )
            return

        # ---- 第二档：超过阈值 ----
        if not self.conf_plat(event, "album_forward_when_exceed", True):
            # 用户关掉了转发，退回「只发前 limit 项 + 提示」
            chain = []
            vi = 0
            ii = 0
            sent = 0
            for kind in kinds:
                if sent >= limit:
                    break
                if kind == "animated" and vi < len(anim_paths):
                    path = anim_paths[vi]
                    vi += 1
                    if path is not None:
                        comp = await self._video_component(path, event)
                        if comp is not None:
                            chain.append(comp)
                            sent += 1
                        else:
                            logger.warning(f"[R插件] 本地动图不可读: {path}")
                elif kind == "still" and ii < len(still_paths):
                    path = still_paths[ii]
                    ii += 1
                    if path is not None:
                        try:
                            chain.append(Comp.Image.fromFileSystem(str(path)))
                            sent += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"[R插件] 本地图片构造失败: {exc}")
            if chain:
                yield event.chain_result(chain)
            yield event.plain_result(f"（共 {len(kinds)} 项，只发了前 {limit} 项）")
            return

        # 合并转发的「发送者」用发起解析的这个用户：昵称 + QQ 号都取发送者
        node_name = (event.get_sender_name() or "").strip() or "解析结果"
        node_uin = str(event.get_sender_id() or "")

        nodes = []
        skipped = 0
        vi = 0
        ii = 0
        for kind in kinds:
            if kind == "animated":
                if vi >= len(anim_paths):
                    continue
                path = anim_paths[vi]
                vi += 1
                if path is None:
                    skipped += 1
                    continue
                comp = await self._video_component(path, event)
                if comp is None:
                    skipped += 1
                else:
                    nodes.append(
                        Comp.Node([comp], name=node_name, uin=node_uin)
                    )
            else:
                if ii >= len(still_paths):
                    continue
                path = still_paths[ii]
                ii += 1
                if path is None:
                    skipped += 1
                    continue
                try:
                    img = Comp.Image.fromFileSystem(str(path))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[R插件] 转发节点图片构造失败: {exc}")
                    skipped += 1
                    continue
                nodes.append(Comp.Node([img], name=node_name, uin=node_uin))

        if not nodes:
            logger.warning(
                f"[R插件][抖音] 图集媒体全部下载失败（共 {len(kinds)} 项），无法合并转发"
            )
            yield event.plain_result(
                f"⚠️ 抖音图集媒体下载失败（共 {len(kinds)} 项），"
                "可能是链接签名过期或 Cookie 失效，可稍后重试"
            )
            return

        if skipped:
            yield event.plain_result(f"（{skipped} 项发送失败，已跳过）")
        yield event.chain_result([Comp.Nodes(nodes)])

    async def _download_images(
        self, event: AstrMessageEvent, result: ResolveResult, urls: list[str]
    ) -> list[Path | None]:
        """下载一组图片，返回与 ``urls`` 等长的路径列表。

        **为什么不能把 URL 直接交给发送端？** 见 ``_download_album_stills``：
        带签名的 CDN 直链需要正确 Referer、而且同一张图有多个候选（哪个 403
        是随机的），AstrBot 发送端两者都不具备，一旦失败是**整条消息链**失败。
        所以图片一律先落到本地再发本地文件。
        """
        if not urls:
            return []

        concurrency = max(
            1, int(self.conf_plat(event, "download_concurrency", 8) or 8)
        )
        max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)
        max_bytes = max_mb * 1024 * 1024

        candidates = result.extra.get("image_candidates")
        if (
            isinstance(candidates, list)
            and len(candidates) == len(urls)
            and all(isinstance(c, list) and c for c in candidates)
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
        return list(paths)

    async def _send_images(self, event: AstrMessageEvent, result: ResolveResult):
        """发送图片列表。图集数量超过 ``max_images`` 时用合并转发完整发出。

        和 ``_send_album`` 一样，图片**先下载到本地再发**：远程签名直链交给
        发送端会因缺少 Referer / 候选回退而整条失败（详见
        ``_download_album_stills`` 的说明）。
        """
        limit = max(1, int(self.conf_plat(event, "max_images", 9) or 9))
        urls = result.images
        total = len(urls)

        # ---- 官机专属：一条（或几条）markdown 内嵌多图 ----
        #
        # **必须先试这条**：官机没有合并转发，所以一旦 ``total > limit`` 就只能
        # 掉进下面的「只发前 N 张」。用户实测过「三张图的图集只出来一张」——
        # 他的 ``plugin.max_images`` 是 1（官机媒体确实一次只能挂一个）。
        # markdown 内嵌图**不看那个阈值**，所以能一条把图全发出来。
        #
        # 图片超过一条能放下的量时**分批发**（每条 MD 最多 ``_MD_IMAGE_TEST_MAX``
        # 张），后续几条不带标题与文案 —— 总比「只发前 N 张」强。
        #
        # 尺寸：抖音走 ``extra['image_sizes']``（解析层白送）；
        # 快手等第三方接口不返回尺寸 → ``probe_missing`` 并发探（Range 64KB）。
        if self._caps(event).markdown and total:
            chunk_size = _MD_IMAGE_TEST_MAX
            chunks = [urls[i:i + chunk_size] for i in range(0, total, chunk_size)]
            mds: list[str] = []
            for n, chunk in enumerate(chunks):
                md = await self._gallery_md_text(
                    event,
                    result,
                    chunk,
                    probe_missing=True,
                    limit=len(chunk),
                    head=(n == 0),
                )
                if not md:
                    mds = []
                    break
                mds.append(md)
            if mds:
                for md in mds:
                    chain = event.chain_result([Comp.Plain(md)])
                    if hasattr(chain, "use_markdown"):
                        chain.use_markdown(True)
                    yield chain
                return
            logger.info(
                f"[R插件] 官机图片未能拼成 MD（共 {total} 张，可能取不到尺寸），退回逐条发送"
            )

        # ---- 不超过阈值：全部下载到本地，一条消息链按顺序发完 ----
        if total <= limit:
            paths = await self._download_images(event, result, urls)
            chain = []
            for path in paths:
                if path is None:
                    continue
                event.track_temporary_local_file(str(path))
                try:
                    chain.append(Comp.Image.fromFileSystem(str(path)))
                except Exception as exc:  # noqa: BLE001 - 单张失败不拖垮整条
                    logger.warning(f"[R插件] 本地图片构造失败: {exc}")
            if chain:
                yield event.chain_result(chain)
            else:
                # 全部失败：明确报出来，别静默什么都不发
                logger.warning(f"[R插件] 图片全部下载失败（共 {total} 张）")
                yield event.plain_result(
                    f"⚠️ {result.platform} 图片下载失败（共 {total} 张），"
                    "可能是链接签名过期或 Cookie 失效，可稍后重试"
                )
            return

        # ---- 超过阈值：合并转发完整发出 ----
        if not self.conf_plat(event, "album_forward_when_exceed", True):
            # 用户关掉了转发，退回「只发前 limit 张 + 提示」的旧行为
            paths = await self._download_images(event, result, urls[:limit])
            chain = []
            for path in paths:
                if path is None:
                    continue
                event.track_temporary_local_file(str(path))
                try:
                    chain.append(Comp.Image.fromFileSystem(str(path)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[R插件] 本地图片构造失败: {exc}")
            if chain:
                yield event.chain_result(chain)
            yield event.plain_result(f"（共 {total} 张，只发了前 {limit} 张）")
            return

        # 并发下载所有图片到本地，用本地文件构造转发节点。
        # 好处：并发（快）+ Node 内部转 base64 时不再重复走网络下载。
        paths = await self._download_images(event, result, urls)

        # 合并转发的「发送者」用发起解析的这个用户：昵称 + QQ 号都取发送者
        node_name = (event.get_sender_name() or "").strip() or "解析结果"
        node_uin = str(event.get_sender_id() or "")

        nodes = []
        skipped = 0
        # 只关心本地路径：`urls` 只是用来保持与 `paths` 一一对应
        for _, path in zip(urls, paths):
            if path is None:
                # 下载失败（防盗链 / 链接过期等）直接跳过，不要用 URL 塞进 Node——
                # Node 转 base64 时会再下载一次，那张图再失败会拖垮整条合并转发。
                skipped += 1
                continue
            event.track_temporary_local_file(str(path))
            try:
                img = Comp.Image.fromFileSystem(str(path))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 转发节点图片构造失败: {exc}")
                skipped += 1
                continue
            nodes.append(Comp.Node([img], name=node_name, uin=node_uin))

        if not nodes:
            # 全部下载失败：明确报出来，别再退回「发 URL」的老路——
            # 那条路会因签名/Referer 问题整条失败，等于什么都没发。
            logger.warning(f"[R插件] 图片全部下载失败（共 {total} 张），无法合并转发")
            yield event.plain_result(
                f"⚠️ {result.platform} 图片下载失败（共 {total} 张），"
                "可能是链接签名过期或 Cookie 失效，可稍后重试"
            )
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
        # 主链接失败时依次试备份 —— 小红书同一视频有多个画质档 / CDN 域名，
        # 单条挂了换一条就能下（见 platforms/xiaohongshu.py 的 video_backups）
        candidates = list(result.videos[:1]) + list(
            result.extra.get("video_backups") or []
        )
        # 大小上限沿用原 Guoba 面板的 videoSizeLimit（单位 MB）
        max_mb = int(self.conf_get("global.videoSizeLimit", 70) or 70)

        for url in candidates:
            try:
                path = await download_media(
                    url, prefix="video", max_bytes=max_mb * 1024 * 1024
                )
                return str(path), ""
            except MediaTooLarge as exc:
                # 超限是明确结论，换备份也一样超，直接放弃
                return None, f"⚠️ {result.platform} 视频过大，已跳过：{exc}"
            except (HttpError, OSError) as exc:
                logger.warning(f"[R插件] 视频下载失败，尝试下一个地址: {exc}")
                continue

        return None, ""

    def _build_video_chain(
        self, event: AstrMessageEvent, result: ResolveResult
    ) -> list:
        """把视频直链拼成消息链。抖音动图会有多条，一次发出去。"""
        limit = max(1, int(self.conf_plat(event, "max_videos", 9) or 9))
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

    # ==================================================================
    # 网易云扫码登录
    # ==================================================================

    def _plugin_version(self) -> str:
        """读 metadata.yaml 里的版本号（读一次缓存住）。"""
        cached = getattr(self, "_version_cache", "")
        if cached:
            return cached
        try:
            text = (Path(__file__).parent / "metadata.yaml").read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("version:"):
                    self._version_cache = line.split(":", 1)[1].strip().strip("\"'")
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件] 读版本号失败: {exc}")
        return getattr(self, "_version_cache", "")

    def _panel_bg_api(self) -> str:
        """图片命令用的随机背景 API；留空或非 http 视为关闭（退回纯色底）。"""
        raw = self.conf_get("plugin.panelBgApi", render_image.DEFAULT_BG_API)
        text = str(raw if raw is not None else render_image.DEFAULT_BG_API).strip()
        return text if text.startswith("http") else ""

    def _cookie_items(self) -> list[tuple[str, str, str]]:
        """``[(平台 key, 中文名, Cookie)]``，供 #cookie状态 用。"""
        out: list[tuple[str, str, str]] = []
        for platform in _COOKIE_FIELDS:
            spec = SPEC_BY_PLATFORM.get(platform)
            label = (spec.label if spec and spec.label else "") or _COOKIE_LABELS.get(
                platform, platform
            )
            out.append((platform, label, self.cookie_for(platform)))
        return out

    async def cmd_netease_scan(self, event: AstrMessageEvent):
        """``#RNQ`` —— 网易云扫码登录。

        走官方网页端接口（``api/login/qrcode/*``），**不需要自建
        NeteaseCloudMusicApi**。扫码成功后把 Cookie 写进 ``music.neteaseCookie``。
        """
        unikey, png = await netease_login.create_login()
        if not unikey or not png:
            yield event.plain_result(
                "❌ 申请网易云登录二维码失败，稍后再试。\n"
                "持续失败的话，可以手动抓 `MUSIC_U` 填进「点歌」分组的**网易云Cookie**。"
            )
            return

        logger.info("[R插件][网易云扫码] 已生成登录二维码")
        yield event.plain_result(
            "请用 **网易云音乐手机客户端** 扫描下面的二维码登录。\n"
            "扫完在手机上点一下「确认登录」，Cookie 会自动写进配置。"
        )
        try:
            # 用 fromBytes（base64），不落临时文件 —— 跨容器发送最稳
            yield event.chain_result([Comp.Image.fromBytes(png)])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][网易云扫码] 二维码发送失败: {exc}")
            yield event.plain_result("二维码发送失败，请重试。")

        umo = event.unified_msg_origin
        timeout = float(self.conf_get("plugin.bili_login_timeout", 180) or 180)
        task = asyncio.create_task(
            self._netease_login_worker(unikey, umo, timeout),
            name="rconsole_netease_login",
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _netease_login_worker(self, unikey: str, umo: str, timeout: float) -> None:
        """后台轮询网易云扫码结果，成功后写配置并回报账号。"""
        try:
            result = await netease_login.wait_for_login(unikey, timeout=timeout)
        except asyncio.CancelledError:
            logger.info("[R插件][网易云扫码] 轮询任务被取消")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件][网易云扫码] 轮询异常: {type(exc).__name__}: {exc}")
            await self._notify(umo, f"❌ 网易云扫码出错：{exc}")
            return

        if result.get("error"):
            await self._notify(umo, f"❌ 网易云扫码失败：{result['error']}")
            return

        cookie = result["cookie"]
        try:
            music = self.conf_data.setdefault("music", {})
            music["neteaseCookie"] = cookie
            saver = getattr(self.conf_data, "save_config", None)
            if callable(saver):
                saver()
            logger.info(
                f"[R插件][网易云扫码] Cookie 已写入配置（{len(cookie)} 字符）"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件][网易云扫码] 写配置失败: {exc}")
            await self._notify(umo, f"❌ 扫码成功但写入配置失败：{exc}")
            return

        account = await netease_login.fetch_account(cookie)
        lines = ["✅ **网易云登录成功，Cookie 已写入配置**"]
        if account.get("ok"):
            lines.append(f"账号：{account['nickname']}（UID {account['user_id']}）")
            vip = account.get("vip_label") or ""
            if account.get("vip_expire"):
                vip = f"{vip}　到期 {account['vip_expire']}"
            if vip:
                lines.append(f"会员：{vip}")
        else:
            lines.append(f"⚠️ 但状态校验没通过：{account.get('msg')}")
        lines.append("现在点歌可以听 VIP 音质了。发 `#cookie状态` 可随时查看。")
        await self._notify(umo, "\n".join(lines))

    def _md_image_width(self, event: AstrMessageEvent | None = None) -> int:
        """MD 内嵌图的显示宽度（px）。

        配置 ``plugin.mdImageWidth``。夹在 100~800 之间 —— 太小看不清、太大
        在手机 MD 框里会撑满一屏。图片按**真实宽高等比**缩放，而 URL 没变，
        所以**点开 / 保存拿到的仍是原图**。

        **多图**（图集 / 多图作品）用的是 ``_md_multi_image_width``。
        """
        try:
            w = int(self.conf_plat(event, "md_image_width", 300) or 300)
        except (TypeError, ValueError):
            w = 300
        return max(100, min(800, w))


    def _md_multi_image_width(self, event: AstrMessageEvent | None = None) -> int:
        """**多图** MD 里单张图的显示宽度（配置 ``plugin.mdMultiImageWidth``）。

        为什么和单图分开：一条 MD 里塞 N 张图时，还用单图那个宽度（默认 300px）
        几张叠起来就把屏幕撑爆了 —— 反而比逐条发更刷屏。默认 **170px**。
        一行放三张时还会再按比例缩（见 ``_gallery_md_text``）。

        尺寸小**不影响点开/保存**：markdown 里写的只是外显宽高，图片按真实比例
        缩放，URL 仍是那张原图。
        """
        try:
            w = int(self.conf_plat(event, "md_multi_image_width", 170) or 170)
        except (TypeError, ValueError):
            w = 170
        return max(80, min(400, w))


    def _qq_buttons_enabled(self, event: AstrMessageEvent | None = None) -> bool:
        """官机菜单按钮的总开关（配置 ``plugin.qqButtons``）。

        为什么留个开关：**自定义按钮在官方文档里标着「内邀开通」**，有些机器人
        可能压根发不出来。留个开关，用户能一键回到「只有图片菜单」的状态。
        """
        return bool(self.conf_plat(event, "qq_buttons", True))

    async def _probe_image_size(self, url: str) -> tuple[int, int] | None:
        """拿图片的「宽×高」—— 只读前 64KB，够 Pillow 解析 header 了。

        **为什么一定要拿尺寸**：QQ markdown 的图片语法是
        ``![alt #宽px #高px](url)``，**尺寸不能省** —— 省了之后手机 QQ
        只渲染成 ``[alt]``（电脑端宽容，照样把图显示出来）。两端表现不一致，
        所以这个坑特别容易漏掉。

        为什么不整张下载：图集动辄十来张 1~2MB 的图，只为量个尺寸不值。
        大多 CDN 支持 Range；不支持就退回全量（也不亏），再失败返回 None，
        调用方退回「不带尺寸」的写法。
        """
        from io import BytesIO

        from .core.http import get_session

        try:
            session = get_session()
            async with session.get(
                url, headers={"Range": "bytes=0-65535"}, timeout=20
            ) as resp:
                if resp.status not in (200, 206):
                    return None
                data = await resp.read()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][MD图] 取尺寸失败 {url[:60]}: {exc}")
            return None
        try:
            from PIL import Image as PILImage

            with PILImage.open(BytesIO(data)) as im:
                return im.size
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][MD图] 解析尺寸失败: {exc}")
            return None

    @staticmethod
    def _md_image(
        url: str,
        size: tuple[int, int] | None,
        *,
        alt: str = "图",
        max_width: int = 300,
    ) -> str:
        """拼一张「MD 内嵌图」。

        **尺寸不能省**（原因见 ``_probe_image_size``）。参数顺序是
        **先宽后高**（实测官方两个示例图都是这个顺序）：

        ==============  ===========  ==================
        图              真实尺寸      官方示例参数
        ==============  ===========  ==================
        building.png    415×640      ``#208px #320px``
        mkd_img.png     928×374      ``#618px #249px``
        ==============  ===========  ==================

        两组比例都吻合到小数点后三位，所以是「宽 高」而不是「高 宽」。
        另外参数值**可以等比缩放**（官方示例本身就是缩过的），所以我们统一
        缩到 ``max_width`` 宽 —— 免得大图在 MD 框里撑满一屏。

        拿不到尺寸时退回不带尺寸的写法（电脑端能看，手机端不显示）——
        有总比没有强，而且用户至少还能看到 alt 文本。
        """
        if not size or size[0] <= 0 or size[1] <= 0:
            return f"![{alt}]({url})"
        w, h = size
        if w > max_width:
            h = max(1, round(h * max_width / w))
            w = max_width
        return f"![{alt} #{w}px #{h}px]({url})"

    # ==================================================================
    # 图片命令：#R菜单 / #cookie状态 / #服务状态
    # ==================================================================
    #
    # 三个命令的耗时都在「外网」上：随机背景约 2.5 秒、头像约 3 秒、
    # Cookie 校验约 1-3 秒。所以统一做法是**先起 task 并行**，最后再一起收，
    # 而不是顺序 await —— 否则用户要等十几秒。

    # ------------------------------------------------------------------
    # QQ 官方机器人：按钮（keyboard）
    # ------------------------------------------------------------------
    #
    # 适配器不认 keyboard，所以只能绕过它自己打接口 —— 见 _send_qq_payload。

    async def _send_qq_payload(self, event: AstrMessageEvent, payload: dict) -> bool:
        """（官机专属）绕过 AstrBot 适配器，直接给 QQ 发原始 payload。

        **为什么必须绕过**：

        1. 适配器**不认 keyboard** —— ``_parse_to_qqofficial`` 只处理
           ``Plain / Image / Record / Video / File``，按钮组件会被静默丢弃；
        2. 它一看到图片就 ``payload.pop("markdown")``（``msg_type`` 改成 7），
           所以「MD + 图片段」永远退化。

        按钮这类适配器没覆盖的形态，只能自己打 HTTP 接口。

        :return: 是否发送成功。**失败时调用方要退回普通路径** ——
            不能因为按钮发不出去就让用户什么都收不到。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        bot = getattr(event, "bot", None)
        if raw is None or bot is None:
            logger.debug("[R插件][按钮] 拿不到 raw_message / bot，跳过")
            return False
        try:
            from botpy.http import Route
        except ImportError:
            logger.debug("[R插件][按钮] 当前环境没有 botpy，跳过")
            return False

        group_openid = getattr(raw, "group_openid", None)
        openid = getattr(getattr(raw, "author", None), "user_openid", None)
        if group_openid:
            route = Route(
                "POST",
                "/v2/groups/{group_openid}/messages",
                group_openid=group_openid,
            )
        elif openid:
            route = Route("POST", "/v2/users/{openid}/messages", openid=openid)
        else:
            logger.debug("[R插件][按钮] 既没有 group_openid 也没有 openid，跳过")
            return False

        body = dict(payload)
        # msg_id = 被动回复（5 分钟内有效、同一个 msg_id 最多回 5 条）；
        # msg_seq 在同一 msg_id 下必须互不相同，否则会被判重丢掉。
        msg_id = getattr(event.message_obj, "message_id", None)
        if msg_id:
            body["msg_id"] = msg_id
        body["msg_seq"] = random.randint(1, 10000)

        try:
            result = await bot.api._http.request(route, json=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][按钮] 发送失败: {type(exc).__name__}: {exc}")
            return False
        if result is None:
            logger.warning("[R插件][按钮] 接口返回 None —— 可能没有自定义按钮权限")
            return False
        return True

    async def _send_md_with_buttons(
        self, event: AstrMessageEvent, markdown: str, rows: list
    ) -> bool:
        """（官机专属）发一条「markdown + 底部按钮」。

        按钮**必须挂在 markdown 消息上**（官方原话：「在 markdown 消息的
        基础上，支持消息最底部挂载按钮」），而且一条消息只能挂一个 keyboard。
        """
        kb = qq_keyboard(*rows)
        if kb is None:
            return False
        return await self._send_qq_payload(
            event,
            {"msg_type": 2, "markdown": {"content": markdown}, "keyboard": kb},
        )

    # ==================================================================
    # 官机语音：自己转 silk、自己上传 —— **超时和重试都由插件决定**
    # ==================================================================
    #
    # 走适配器那条路（Record → to_path(tencent_silk) → upload_group_and_c2c_media）
    # 有个要命的地方：botpy 的 BotHttp.timeout 是**实例级**属性，超时后 request()
    # 直接返回 None，被 APIReturnNoneError 接住交给 tenacity 重试 3 次（退避 2/4/8s）。
    # 腾讯接口一抖，用户就要干等 90+ 秒才被告知失败 —— 实测日志：
    #
    #     15:28:46 语音预转码完成 → 15:29:21 首次失败 → 15:30:12 第三次重试
    #
    # 自己走这条路，失败能在 30 秒内暴露并降级成链接。

    async def _upload_qq_media(
        self,
        event: AstrMessageEvent,
        data: bytes,
        file_type: int,
        *,
        timeout: float = _QQ_UPLOAD_TIMEOUT,
    ) -> str:
        """（官机专属）上传富媒体，拿 ``file_info``；**超时由插件自己定**。

        :param timeout: 上传超时（秒）。默认见 ``_QQ_UPLOAD_TIMEOUT`` ——
            实测「冷却后首次上传」约 8 秒，25 秒留了三倍余量；而 botpy 的默认
            15 秒配上 tenacity 重试 3 次会让用户干等 90+ 秒。
        :return: ``file_info``；失败返回空串。**绝不抛异常**。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        bot = getattr(event, "bot", None)
        if raw is None or bot is None:
            return ""
        try:
            from botpy.http import Route
        except ImportError:
            return ""

        group_openid = getattr(raw, "group_openid", None)
        openid = getattr(getattr(raw, "author", None), "user_openid", None)
        body = {
            "file_data": base64.b64encode(data).decode("ascii"),
            "file_type": file_type,
            "srv_send_msg": False,  # 只上传；发送由 _send_qq_payload 负责
        }
        if group_openid:
            body["group_openid"] = group_openid
            route = Route(
                "POST", "/v2/groups/{group_openid}/files", group_openid=group_openid
            )
        elif openid:
            body["openid"] = openid
            route = Route("POST", "/v2/users/{openid}/files", openid=openid)
        else:
            return ""

        http = getattr(getattr(bot, "api", None), "_http", None)
        if http is None:
            return ""

        # botpy 的 timeout 没有 per-request 参数，只能临时改实例属性、用完恢复。
        # 官机上传是低频操作（一次点歌一次），撞车概率可忽略。
        saved = getattr(http, "timeout", None)
        try:
            http.timeout = timeout
            result = await http.request(route, json=body)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[R插件][语音] 上传异常: {type(exc).__name__}: {exc}")
            return ""
        finally:
            if saved is not None:
                http.timeout = saved

        if isinstance(result, dict):
            return str(result.get("file_info") or "")
        logger.info(f"[R插件][语音] 上传没返回 dict: {str(result)[:120]}")
        return ""

    async def _send_qq_voice(self, event: AstrMessageEvent, wav_path) -> bool:
        """（官机专属）本地转 silk → 上传 → 发语音条。

        :param wav_path: **16kHz 单声道 16bit** 的 wav（``_music_to_voice_wav``
            的产物）。格式不符会直接返回 False —— 不在热路径上偷偷重采样。
        :return: 是否成功。**失败时调用方必须降级发链接**，别让用户空等。
        """
        if not qq_silk_available():
            logger.debug("[R插件][语音] 环境没有 pysilk，退回适配器路径")
            return False

        try:
            # pysilk 是同步 C 调用，一首 4 分半的歌要跑 20 秒 ——
            # 放主线程会把整个 event loop 堵死（这正是我们要绕开的坑）。
            silk = await asyncio.to_thread(qq_encode_silk, str(wav_path))
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[R插件][语音] silk 编码异常: {type(exc).__name__}: {exc}")
            return False
        if not silk:
            logger.info("[R插件][语音] silk 编码失败（格式不符或无 pysilk），退回适配器路径")
            return False
        try:
            src_kb = Path(wav_path).stat().st_size // 1024
        except OSError:  # 文件刚好被清掉也不该让日志把这次发送带崩
            src_kb = 0
        logger.info(
            f"[R插件][语音] silk 编码完成: {len(silk) // 1024}KB（源 wav {src_kb}KB）"
        )

        t0 = time.monotonic()
        # 实测（2026-09-23，414KB silk）：静置后首次上传 **8.28 秒**；
        # 紧接着再传一次则是 46.36 秒 —— 腾讯侧对连续上传会**排队**，而且
        # 耗时与体积无关（561KB 反而比 293KB 快）。
        # 用户点歌是零散的，正常命中「首次」那一档，所以 25 秒足够从容。
        file_info = await self._upload_qq_media(event, silk, QQ_VOICE_FILE_TYPE)
        elapsed = time.monotonic() - t0

        # 只对「秒失败」重试：那多半是瞬时错误。耗满超时的说明被排队了，
        # 再试一次只会更慢，还会让别人排得更久。
        if not file_info and elapsed < _QQ_UPLOAD_FAST_FAIL:
            logger.info(
                f"[R插件][语音] 秒失败（{elapsed:.1f}s），1.5 秒后重试一次"
            )
            await asyncio.sleep(1.5)
            t0 = time.monotonic()
            file_info = await self._upload_qq_media(event, silk, QQ_VOICE_FILE_TYPE)
            elapsed = time.monotonic() - t0

        if not file_info:
            logger.info(
                f"[R插件][语音] 上传失败（{elapsed:.1f}s），降级发链接"
            )
            return False
        logger.info(
            f"[R插件][语音] 上传成功（{elapsed:.1f}s）file_info={file_info[:18]}…"
        )

        # content=None 是照着适配器抄的：它发富媒体时一定会带这个字段，
        # 缺了服务端可能把这条判成无效（表现是客户端「加载失败」）。
        return await self._send_qq_payload(
            event,
            {
                "msg_type": 7,
                "media": {"file_info": file_info},
                "content": None,
            },
        )

    def _menu_button_rows(self) -> list:
        """菜单按钮的行列设计。

        「点歌」故意用 ``enter=False``：点击后只把 ``点歌 `` **填进输入框**，
        等用户补歌名再发 —— 这正是「添加参数再发送」的用法。
        其余按钮 ``enter=True``（单聊直接发；群里会插进输入框，同样能用）。
        """
        return [
            [
                qq_button("R菜单", "#R菜单", enter=True),
                qq_button("视频解析", "#R解析"),
                qq_button("点歌", "点歌 ", enter=False),
            ],
            [
                qq_button("R配置", "#R配置", enter=True, style=0),
                qq_button("状态", "#cookie状态", style=0),
                qq_button("免艾特", "#R免艾特", style=0),
            ],
        ]

    def _menu_markdown(self, bot_name: str = "") -> str:
        """官机版菜单：**纯 markdown**，不发图片。

        为什么官机不发菜单图：图是**本地渲染的 PNG**，没有公网 URL，塞不进
        markdown；而按钮只能挂 markdown 消息。实测「media + 按钮」的组合也不
        成立（客户端报「图片加载失败」）—— 所以官机干脆用 MD 排版，
        功能说明一点不少，还能带按钮。
        """
        names = "、".join(r.name for r in AUTO_RULES if r.enabled)
        lines = [
            "# 🎵 R插件 · 功能菜单",
            "",
            "## 🔗 链接解析",
            "发链接即触发，不用加命令",
            names,
            "",
            "## 🎵 点歌",
            "`点歌 歌名` · `网易云点歌 歌名` · `QQ点歌 歌名`",
            "搜到后**点下面的按钮**，或直接回序号",
            "",
            "## 🔍 查询",
            "`#cookie状态`　Cookie 失效 + 会员等级",
            "`#服务状态`　　服务器负载与 Bot 信息",
            "`#R平台`　　　 本协议端能发什么",
            "",
            "## ⚙️ 管理员",
            "`#R配置` 全部配置项（平台 / 形式 / Cookie / 点歌）",
            "`#RNQ` 网易云扫码 · `#RBQ` B站扫码 · `#RBS` B站状态",
        ]
        if bot_name:
            lines += ["", f"— {bot_name}"]
        return "\n".join(lines)

    async def cmd_r_menu(self, event: AstrMessageEvent):
        """``#R菜单`` —— **一张图 + 下面一排功能按钮**。

        **官机**：随机图 → 图床唯一直链 → 一条 markdown（内嵌图 + 按钮）。
        为什么要绕一趟图床（而不是直接把随机图 API 的地址塞进 MD）：

        * markdown 内嵌图**必须带尺寸**，而随机图 API 每次给的是**另一张**图
          —— 尺寸对不上就会变形（实测 ``/random/`` 的比例极差有 1.08）；
        * 同一个 URL 会被客户端**缓存**，菜单图就永远是同一张了。

        所以先落地（读真实尺寸）再上传拿**唯一**地址，两个问题一起解决。

        降级链（哪一步断了都往后退，不会什么都不发）：
        ① 随机图 + 按钮 → ② 纯文字 MD 菜单 + 按钮 → ③ 本地渲染的功能图。
        """
        caps = self._caps(event)
        name_task = asyncio.create_task(fetch_bot_name(event))
        bot_name = await name_task
        want_buttons = caps.keyboard and self._qq_buttons_enabled(event)
        rows = self._menu_button_rows() if want_buttons else []

        if want_buttons:
            # ① 随机图 + 按钮（要的就是这个）
            md = await self._menu_image_md(event)
            if md and await self._send_md_with_buttons(event, md, rows):
                return
            # ② 纯文字 MD 菜单 + 按钮
            if await self._send_md_with_buttons(
                event, self._menu_markdown(bot_name), rows
            ):
                logger.info("[R插件][菜单] 随机图那条路不通，已发纯文字 MD 菜单")
                return
            logger.info("[R插件][菜单] MD 菜单发送失败，退回图片菜单")

        bg_api = self._panel_bg_api()
        bg_task = asyncio.create_task(render_image.fetch_background(bg_api))
        await bg_task

        png = await render_menu(self._plugin_version(), bot_name, bg_api)
        if png is None:
            yield event.plain_result(self._menu_text(bot_name, event))
        else:
            yield event.chain_result([Comp.Image.fromBytes(png)])

    async def _menu_image_md(self, event: AstrMessageEvent) -> str:
        """（官机）菜单的 markdown：**一张随机图，不配文字**（用户指定）。

        四步，每步都有原因：

        1. 取图 —— ``plugin.menuImageApi``（默认 elaina 的**竖屏**档）；
        2. **交给 czoss 转存** —— 换成国内节点、**内容固定**的直链。
           这一步是关键：官机 markdown 的图是**腾讯服务器下载转存**的，
           图放在 Cloudflare（freeimage.host → ``iili.io``）上实测客户端报
           「图片加载失败」，换到国内 OSS 才通。顺带 URL 每次不同，
           不会被客户端缓存（否则菜单图永远是同一张）；
        3. **量真实尺寸** —— markdown 内嵌图不带尺寸时手机 QQ 只渲染 ``[alt]``；
        4. 拼 MD。

        转存不通就退回老路径（① 下载随机图 → ② 传图床 → ③ 拼 MD），
        再不行返回空串，调用方走纯文字菜单降级。
        """
        api = self._menu_image_api()
        if not api:
            return ""

        url = await transfer_url(api, key=self._czoss_key())
        if url:
            size = await self._probe_image_size(url)
            if size:
                logger.info(
                    f"[R插件][菜单] 随机图转存就位 {size[0]}x{size[1]} -> {url}"
                )
                return self._md_image(
                    url, size, alt="菜单", max_width=self._md_image_width(event)
                )
            logger.debug("[R插件][菜单] 转存直链量不到尺寸，退回图床路径")

        return await self._menu_image_md_by_bed(event, api)

    async def _menu_image_md_by_bed(
        self, event: AstrMessageEvent, api: str
    ) -> str:
        """降级路径：下载随机图 → 传图床 → 拼 MD。

        图床在 Cloudflare 上，腾讯侧**可能取不到**（这就是当初「图片加载失败」
        的根因），所以只当兜底 —— 转存接口通了就轮不到这里。
        """
        try:
            from io import BytesIO

            from PIL import Image as PILImage
        except ImportError:
            logger.debug("[R插件][菜单] 没有 Pillow，读不了随机图尺寸")
            return ""

        from .core.http import fetch as http_fetch

        try:
            body, _ = await http_fetch(api, retries=1, timeout=25.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][菜单] 随机图取不到: {type(exc).__name__}: {exc}")
            return ""
        if not body:
            return ""

        try:
            with PILImage.open(BytesIO(body)) as im:
                size = im.size
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][菜单] 随机图读不出尺寸: {exc}")
            return ""

        url = await upload_image(body, key=self._image_bed_key())
        if not url:
            return ""

        logger.info(
            f"[R插件][菜单] 随机图就位（图床兜底）{size[0]}x{size[1]} "
            f"({len(body) // 1024}KB) -> {url}"
        )
        return self._md_image(
            url, size, alt="菜单", max_width=self._md_image_width(event)
        )


    def _menu_image_api(self) -> str:
        """菜单随机图 API（``plugin.menuImageApi``）。

        默认用 ``api.elaina.cat`` 的**竖屏**档 —— 实测三档的比例极差：

        ================  ==================  ========
        端点              比例范围             极差
        ================  ==================  ========
        ``/random/``      1.522 ~ 2.604       **1.082**（横竖混着给）
        ``/random/mobile`` 0.646 ~ 0.750      0.104 ← 用这个
        ``/random/pc``    1.723 ~ 2.604       0.880
        ================  ==================  ========

        反正尺寸是本地读出来再写进 markdown 的，这里只挑「稳」的那档。
        留空 / 非 http 视为关闭（直接走降级链，不发图）。
        """
        raw = self.conf_get("plugin.menuImageApi", _MENU_IMAGE_API_DEFAULT)
        text = str(raw if raw is not None else _MENU_IMAGE_API_DEFAULT).strip()
        return text if text.startswith("http") else ""

    def _image_bed_key(self) -> str:
        """图床 API key（``plugin.imageBedKey``）；留空则用公开测试 key。"""
        return str(self.conf_get("plugin.imageBedKey", "") or "").strip()

    def _czoss_key(self) -> str:
        """转存接口的 API key（``plugin.czossKey``）—— 实测留空也能用。"""
        return str(self.conf_get("plugin.czossKey", "") or "").strip()


    async def cmd_no_at(self, event: AstrMessageEvent):
        """``#R免艾特`` —— 教用户开启「群内全量消息」（官机专属）。

        为什么会需要这条：QQ 官方机器人在群里**默认只收 @ 到自己的消息**，
        用户很容易以为「插件坏了 / 不响应」。实际那是手机 QQ 里的一个开关，
        **机器人自己改不了、也读不到**，只能把路径讲清楚。

        为什么限定官机：OneBot 系列（NapCat 等）本来就能收到群内全部消息，
        对它讲这个只会误导。

        文案来源：用户提供的另一个机器人的实际输出（步进式 + 版本要求），
        按用户要求**去掉了「备选：点击这里输入群号」那条**。
        """
        caps = self._caps(event)
        if caps.key != "qqofficial":
            yield event.plain_result(
                f"ℹ️ 当前是 {caps.label} —— 它没有「群消息范围」这道限制，不用设置。"
            )
            return

        text = (
            "📢 群里「不用 @ 也能用」怎么开\n\n"
            "1. 请群主**点击我的头像** →\n"
            "2. 点击**右上角齿轮**（设置）→\n"
            "3. 把「可获取的群聊消息范围」设为「**获取群内全部消息**」→\n"
            "4. 勾选「**主动在群聊内发言**」即可\n\n"
            "> 授权后无需 @ 机器人也可以使用\n"
            "> 需要在 **9.2.90 以上版本 QQ** 里设置"
        )
        chain = event.chain_result([Comp.Plain(text)])
        if hasattr(chain, "use_markdown"):
            chain.use_markdown(True)
        yield chain

    async def cmd_resolve_help(self, event: AstrMessageEvent):
        """``#R解析`` —— 链接解析怎么用（菜单按钮「视频解析」指向它）。

        单独做一条命令的原因：菜单按钮总得填点什么，而解析本身是「发链接即
        触发」、没有命令可填 —— 那就让它落到这条说明上，顺便也是「为什么我
        发的链接没反应」的自查入口。
        """
        names = [r.name for r in AUTO_RULES if r.enabled]
        lines = [
            "🔗 链接解析",
            "",
            "用法：**直接把分享链接发给我**，不用加任何命令。",
            f"已开启：{'、'.join(names) if names else '（全部关闭）'}",
            "",
            "没反应时按这个顺序看：",
            "· `#R平台` —— 本协议端能发什么（官方机器人没有合并转发 / 音乐卡片）",
            "· `#R配置` —— 对应平台有没有被关掉",
        ]
        yield event.plain_result("\n".join(lines))

    def _menu_text(
        self, bot_name: str = "", event: AstrMessageEvent | None = None
    ) -> str:
        """图片渲染不可用时的文字菜单兜底。

        ``event`` 只用来判断「当前发送形式」；拿不到就按直发算 ——
        官方机器人本来就只能直发，所以这个默认值是无害的。
        """
        lines = ["🎵 R插件 · 功能菜单", ""]
        lines.append("【链接解析】发链接即触发")
        lines.append("　" + "、".join(r.name for r in AUTO_RULES if r.enabled))
        lines.append("")
        lines.append("【点歌】")
        lines.append("　点歌 <歌名>　　　　　按默认平台搜")
        lines.append("　网易云点歌 <歌名>　　只搜网易云")
        lines.append("　QQ点歌 <歌名>　　　　只搜 QQ 音乐")
        lines.append("　（接着发 1/2/3）　　 60 秒内回序号播放")
        lines.append("")
        lines.append("【查询】")
        lines.append("　#cookie状态　所有 Cookie 是否失效 + 会员等级")
        lines.append("　#服务状态　　服务器负载与 Bot 信息")
        lines.append("　#R菜单　　　 本菜单")
        lines.append("")
        lines.append("【管理员】")
        lines.append("　#RNQ　网易云扫码登录")
        lines.append("　#RBQ　B站扫码登录　#RBS　B站登录状态")
        lines.append("")
        # 配置类命令统一收在 #R配置 一个入口下；这里顺手把「当前发送形式」
        # 和「切到另一个形式」的命令并排显示，省得用户去记当前是哪个
        form_now = (
            "聊天记录" if event is not None and self._forward_enabled(event) else "直发"
        )
        form_to = "直发" if form_now == "聊天记录" else "聊天记录"
        lines.append("【管理员配置】发 #R配置 帮助 看全部")
        lines.append("　#R配置 平台 抖音 开|关　　开关某个平台的解析")
        lines.append(f"　#R配置 形式 {form_to}　　　当前：{form_now}")
        lines.append("　#R配置 cookie 小红书　　　私聊设置 Cookie（两步）")
        lines.append("　#R配置 点歌 平台 网易云　　默认平台 / 数量 / 发送方式")
        if bot_name:
            lines.append("")
            lines.append(f"— {bot_name}")
        return "\n".join(lines)

    async def cmd_cookie_status(self, event: AstrMessageEvent):
        """``#cookie状态`` —— 查所有 Cookie 是否失效（图片简略展示）。"""
        bg_api = self._panel_bg_api()
        bg_task = asyncio.create_task(render_image.fetch_background(bg_api))
        rows, stamp = await check_all_cookies(self._cookie_items())
        await bg_task

        png = await render_cookie_status(rows, stamp, bg_api)
        if png:
            yield event.chain_result([Comp.Image.fromBytes(png)])
            return
        yield event.plain_result(self._cookie_text(rows, stamp))

    def _cookie_text(self, rows: list[dict], stamp: str) -> str:
        """图片渲染不可用时的文字兜底。"""
        mark = {"ok": "✅", "bad": "❌", "warn": "⚠️", "off": "➖"}
        lines = [f"🔑 Cookie 状态（{stamp}）", ""]
        for r in rows:
            head = f"{mark.get(r['status'], '·')} {r['label']}"
            if r.get("nickname"):
                head += f"　{r['nickname']}"
            lines.append(head)
            if r.get("detail"):
                lines.append(f"　　{r['detail']}")
        return "\n".join(lines)

    async def cmd_service_status(self, event: AstrMessageEvent):
        """``#服务状态`` —— 服务器负载 + Bot 信息（图片）。"""
        bg_api = self._panel_bg_api()
        self_id = self_id_of(event)

        # 三件事并行：随机背景 / 头像 / Bot 昵称；系统指标是纯本地读取，
        # 顺手在这段等待里做完
        bg_task = asyncio.create_task(render_image.fetch_background(bg_api))
        avatar_task = asyncio.create_task(fetch_avatar(self_id))
        name_task = asyncio.create_task(fetch_bot_name(event))
        base = collect_system()
        avatar = await avatar_task
        bot_name = await name_task
        await bg_task

        enabled = self.conf_get("plugin.enabled_platforms", [])
        if isinstance(enabled, (list, tuple)):
            enabled_text = f"{len(enabled)} 个"
        else:
            enabled_text = "—"

        info = {
            "host": _platform.node() or "未知主机",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bot_qq": self_id or "未知",
            "bot_name": bot_name,
            "avatar": avatar,
            "platform": event.get_platform_name() or "—",
            "enabled_platforms": enabled_text,
            "version": self._plugin_version(),
            "cmd_count": len(COMMAND_RULES),
            "auto_count": len(AUTO_RULES),
            "loads": base["loads"],
            "env_lines": base["env_lines"],
            "footer": "数据由 psutil 实时采集",
        }
        png = await render_service_status(info, bg_api)
        if png:
            yield event.chain_result([Comp.Image.fromBytes(png)])
            return
        yield event.plain_result(self._service_text(info))

    def _service_text(self, info: dict) -> str:
        lines = ["🖥️ 服务状态", ""]
        lines.append(f"Bot　QQ {info['bot_qq']}　{info.get('bot_name') or ''}")
        lines.append(f"平台　{info.get('platform')}　·　插件 v{info.get('version')}")
        lines.append("")
        for item in info.get("loads", []):
            lines.append(f"{item['label']}：{item['text']}")
        lines.append("")
        lines.extend(info.get("env_lines", []))
        return "\n".join(lines)

    # ==================================================================
    # #R配置 —— 聊天里改常用配置（仅管理员）
    # ==================================================================
    #
    # 起因：常用开关（某个平台要不要解析、点歌默认平台、发送形式）原本只能去
    # WebUI 的插件配置里翻分组找，改一次要开网页。这里把它们搬到聊天里，
    # 顺手把「设 Cookie」做成私信两步式 —— Cookie 是凭据，在群里粘贴等于公开。

    # 点歌「发送方式」可选值：词 -> 配置值
    _MUSIC_SEND_ALIAS: dict[str, str] = {
        "链接": "link", "link": "link", "列表": "link",
        "卡片": "card", "card": "card",
        "语音": "voice", "voice": "voice", "音频": "voice",
        "文件": "file", "file": "file",
    }
    # 点歌「方式」：列表（发列表图再回序号）或直接送（直接发第一首）
    _MUSIC_SEARCH_ALIAS: dict[str, str] = {
        "列表": "list", "list": "list", "搜索": "list",
        "直接": "direct", "direct": "direct", "单曲": "direct", "直发": "direct",
    }
    # 点歌默认平台
    _MUSIC_PLATFORM_ALIAS: dict[str, str] = {
        "netease": "netease", "网易云": "netease", "网抑云": "netease", "网易": "netease",
        "qqmusic": "qqmusic", "qq音乐": "qqmusic", "qq": "qqmusic",
    }
    _MUSIC_LABELS: dict[str, str] = {"netease": "网易云", "qqmusic": "QQ音乐"}

    _ON_WORDS = frozenset({"开", "开启", "启用", "打开", "on", "1", "true", "是", "yes"})
    _OFF_WORDS = frozenset({"关", "关闭", "禁用", "off", "0", "false", "否", "no"})

    async def cmd_r_config(self, event: AstrMessageEvent):
        """``#R配置`` —— 在聊天里改常用配置（仅管理员）。

        ==============================================  ================================
        命令                                            作用
        ==============================================  ================================
        ``#R配置``                                      总览
        ``#R配置 帮助``                                  完整用法
        ``#R配置 平台``                                  所有平台开关状态
        ``#R配置 平台 抖音 开|关``                        开关某平台的自动解析
        ``#R配置 cookie``                                各平台 Cookie 配置情况
        ``#R配置 cookie 小红书``                         私信里进入「等你发 Cookie」
        ``#R配置 cookie 小红书 <整串>``                   直接设置（前面加「强制」跳过体检）
        ``#R配置 cookie 小红书 清除``                     清空
        ``#R配置 点歌 平台|数量|发送|方式|开关 <值>``       点歌相关设置
        ``#R配置 形式 聊天记录|直发``                      解析内容发送形式
        ==============================================  ================================

        为什么「发送形式」值得单独一个开关：内容多的时候合并成一条聊天记录
        不刷屏；想让内容一眼可见时直发更合适。两种都有人要，所以做成可切换的，
        默认直发（跟升级前一致）。
        """
        text = event.get_message_str().strip()
        rest = _RCONFIG_PREFIX.sub("", text, count=1).strip()
        parts = rest.split()
        sub = parts[0].lower() if parts else ""
        # 子命令之后的**原始**文本（保留内部空格）。Cookie 必须用这份：
        # 先 split 再拼回去会把 `a=1; b=2` 里的空格吃掉
        tail = re.sub(r"^\S+\s*", "", rest, count=1) if sub else ""

        try:
            if not sub:
                yield event.plain_result(self._cfg_overview(event))
            elif sub in ("帮助", "help", "?", "？", "h"):
                yield event.plain_result(self._cfg_help())
            elif sub in ("平台", "platform", "pf"):
                yield event.plain_result(self._cfg_platform(parts[1:]))
            elif sub in ("协议端", "机器人", "bot", "profile", "档"):
                yield event.plain_result(self._cfg_profile(event))
            elif sub in ("cookie", "ck"):
                async for item in self._cfg_cookie(event, tail):
                    yield item
            elif sub in ("点歌", "music", "歌"):
                yield event.plain_result(self._cfg_music(event, parts[1:]))
            elif sub in ("形式", "发送形式", "转发", "form", "send"):
                yield event.plain_result(self._cfg_form(event, parts[1:]))
            else:
                yield event.plain_result(
                    f"❓ 不认识「{parts[0]}」，发「#R配置 帮助」看全部用法。"
                )
        except Exception as exc:  # noqa: BLE001 - 聊天命令出错也不该炸给框架
            logger.error(f"[R插件][R配置] 执行出错: {type(exc).__name__}: {exc}")
            yield event.plain_result(f"❌ 执行出错：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 总览 / 帮助
    # ------------------------------------------------------------------

    def _cfg_overview(self, event: AstrMessageEvent) -> str:
        enabled = [r.name for r in AUTO_RULES if r.key in self._enabled_keys()]
        mode = "聊天记录（合并转发）" if self._forward_enabled(event) else "直发（多条消息）"
        caps = self._caps(event)

        music_on = "开" if self.conf_get("music.enable", False) else "关"
        m_platform = self._music_label(str(self.conf_get("music.platform", "netease") or "netease"))
        m_count = self.conf_get("music.maxList", 10)
        # 发送方式 / 点歌方式是**按协议端**读的：官机没有音乐卡片，那份默认就是语音
        m_send = str(self.conf_plat(event, "music_send_mode", "link") or "link")
        m_search = (
            "列表"
            if str(self.conf_plat(event, "music_search_mode", "list") or "list") == "list"
            else "直接送"
        )

        lines = [
            "⚙️ R插件配置总览",
            "",
            f"🧩 当前协议端：{caps.label}（{caps.key}）",
            f"📤 解析发送形式：**{mode}**",
            f"🧩 自动解析平台：{len(enabled)}/{len(AUTO_RULES)} 个启用",
            f"　　　{_brief_names(enabled)}",
            f"🎵 点歌：{music_on}　默认 {m_platform}　列表 {m_count} 首　{m_search}　发送方式 {m_send}",
            "",
            "💡 上面这些值取自**当前协议端**那份配置。"
            "看完整一份：`#R配置 协议端`",
            "",
            self._cookie_brief(),
            "",
            "改配置：#R配置 帮助",
        ]
        return "\n".join(lines)

    def _cfg_profile(self, event: AstrMessageEvent) -> str:
        """``#R配置 协议端`` —— 看**当前协议端**这份配置档的生效值。

        为什么要单独一条：配置面板里是几组并列的，用户在官机里改了值却不确定
        到底读的哪一份。这条命令按**你说话的这个机器人**回答，不会看错。
        """
        caps = self._caps(event)
        return "\n".join([
            "🧩 分协议端配置",
            "",
            describe_profiles(self._platform_profiles(), caps.key, self.conf_get),
            "",
            "为什么分两份：OneBot 能发合并转发和音乐卡片，QQ 官方机器人两样都没有"
            "（但换来了原生 markdown + 消息按钮）。混在一起改，"
            "官机上永远有一半选项是无效的。",
            "",
            "改值：WebUI → 插件配置 → 分协议端配置",
            "　　　（也可用 `#R配置 形式` / `#R配置 点歌 发送` 快捷改**当前这份**）",
        ])

    def _cfg_help(self) -> str:
        return "\n".join([
            "⚙️ #R配置 · 用法（仅管理员，带 # 或 / 前缀）",
            "",
            "【协议端】",
            "　#R配置 协议端　　　　　　看**当前机器人**这份配置（OneBot / 官机各一份）",
            "　#R配置 形式 聊天记录|直发　只改当前协议端那份",
            "　#R配置 点歌 发送 <值>　　　只改当前协议端那份",
            "",
            "【解析】",
            "　#R配置 平台　　　　　　　列出所有平台的开关状态",
            "　#R配置 平台 抖音 关　　　关掉抖音的自动解析（改「开」则打开）",
            "　#R配置 形式 聊天记录　　　解析内容打包成一条聊天记录发出",
            "　#R配置 形式 直发　　　　　改回「简介 + 媒体」多条消息直发",
            "",
            "【Cookie】（设置类只能在私聊里做）",
            "　#R配置 cookie　　　　　　查看各平台是否已配置",
            "　#R配置 cookie 小红书　　 我提示后，你把整串 Cookie 发过来",
            "　#R配置 cookie 小红书 <串>　直接设置；不必完整，但要有必备字段",
            "　#R配置 cookie 小红书 强制 <串>　跳过必备字段检查强行写入",
            "　#R配置 cookie 小红书 清除　清空该平台的 Cookie",
            "",
            "【点歌】",
            "　#R配置 点歌 平台 网易云｜QQ音乐",
            "　#R配置 点歌 数量 10　　　列表显示几首",
            "　#R配置 点歌 发送 链接｜卡片｜语音｜文件",
            "　#R配置 点歌 方式 列表｜直接",
            "　#R配置 点歌 开关 开｜关",
            "",
            "平台名支持中文名或内部 key（抖音 / douyin 都认）。",
        ])

    # ------------------------------------------------------------------
    # 平台开关
    # ------------------------------------------------------------------

    def _cfg_platform(self, args: list[str]) -> str:
        enabled = self._enabled_keys()

        # 不带参数：列状态（格式对齐，方便一眼扫）
        if not args:
            lines = ["🧩 平台开关（✅ 已启用 / ⬜ 未启用）", ""]
            for rule in AUTO_RULES:
                mark = "✅" if rule.key in enabled else "⬜"
                lines.append(f"{mark} {rule.name}　（{rule.key}）")
            lines.append("")
            lines.append(f"共 {len(enabled)}/{len(AUTO_RULES)} 个启用")
            lines.append("用法：#R配置 平台 <平台名> 开｜关")
            return "\n".join(lines)

        if len(args) < 2:
            return "❓ 用法：#R配置 平台 <平台名> 开｜关\n例如：`#R配置 平台 抖音 关`"

        # 平台名里可能有空格，所以约定「最后一个词是开/关，前面全是平台名」
        action = args[-1].lower()
        name = "".join(args[:-1])
        key = _PLATFORM_ALIAS.get(name.lower())
        if not key:
            return (
                f"❓ 不认识平台「{name}」。\n"
                "发 `#R配置 平台` 可以看到全部平台的准确名字。"
            )

        if action in self._ON_WORDS:
            turning_on = True
        elif action in self._OFF_WORDS:
            turning_on = False
        else:
            return f"❓ 只认「开」或「关」，收到的是「{args[-1]}」。"

        keys = self._enabled_keys()
        if turning_on:
            keys.add(key)
        else:
            keys.discard(key)

        # 按 AUTO_RULES 的顺序存，WebUI 里勾选框的顺序才稳定
        ordered = [r.key for r in AUTO_RULES if r.key in keys]
        err = self._save_conf("plugin.enabled_platforms", ordered)
        if err:
            return f"❌ 保存失败：{err}"

        rule = next((r for r in AUTO_RULES if r.key == key), None)
        label = rule.name if rule else key
        if turning_on:
            return f"✅ 已开启 **{label}** 的自动解析（当前 {len(ordered)} 个平台启用）"
        return (
            f"✅ 已关闭 **{label}** 的自动解析（当前 {len(ordered)} 个平台启用）\n"
            "以后这个平台的链接不再自动处理。"
        )

    # ------------------------------------------------------------------
    # Cookie
    # ------------------------------------------------------------------

    async def _cfg_cookie(self, event: AstrMessageEvent, rest: str):
        """Cookie 子命令：查看 / 设置（两步式或直接带值） / 清除。

        参数从**原始文本**里切，而不是 ``split()`` 之后的词表：Cookie 里有
        ``; `` 这种带空格的分隔符，先按空白切成词再拼回去会把空格吃掉，
        写进配置的串就变味了（实测踩过：``a=1; b=2`` 被写成 ``a=1;b=2``）。
        """
        text = str(rest or "").strip()
        if not text:
            yield event.plain_result(self._cookie_config_overview())
            return

        match = re.match(r"^(\S+)(?:\s+([\s\S]*))?$", text)
        if not match:
            yield event.plain_result(self._cookie_config_overview())
            return
        name = match.group(1)
        tail = (match.group(2) or "").strip()

        key = _COOKIE_ALIAS.get(name.lower())
        if not key:
            yield event.plain_result(
                f"❓ 不认识平台「{name}」。可设置的平台：\n"
                "　" + "、".join(self._cookie_label(p) for p in _COOKIE_FIELDS)
            )
            return

        label = self._cookie_label(key)

        # ---- 不带值：进入「等 Cookie」状态（两步式）----
        if not tail:
            if not event.is_private_chat():
                yield event.plain_result(
                    "🔒 Cookie 只能在**私聊**里设置，免得发到群里被别人拿走。\n"
                    f"请私聊我发：`#R配置 cookie {name}`"
                )
                return

            umo = event.unified_msg_origin
            # 登记等待状态（同步操作，放在第一个 await 之前）
            self._cookie_pending[umo] = (time.time() + _COOKIE_PENDING_TTL, key)
            need = " 或 ".join(COOKIE_REQUIRED_ANY.get(key) or ()) or "任意 key=value"
            yield event.plain_result(
                f"⌛ 请在 **{int(_COOKIE_PENDING_TTL // 60)} 分钟**内，把 {label} 的 Cookie 整串"
                "发给我（浏览器 F12 → Network → 随便点一个请求 → 复制请求头里的 Cookie）。\n"
                f"· 不要求完整，但必须包含 **{need}**\n"
                "· 发「取消」可以放弃"
            )
            return

        # 「强制」前缀：跳过必备字段体检
        force = False
        value = tail
        fm = re.match(r"^(?:强制|force|-f)(?:\s+([\s\S]*))?$", tail, re.IGNORECASE)
        if fm:
            force = True
            value = (fm.group(1) or "").strip()

        if value in ("清除", "清空", "删除", "clear", "reset"):
            yield event.plain_result(self._clear_cookie(key))
            return

        if not event.is_private_chat():
            yield event.plain_result(
                "🔒 设置 Cookie 请私聊我操作，别在群里贴凭据。\n"
                f"私聊发：`#R配置 cookie {name}`"
            )
            return

        yield event.plain_result(self._set_cookie(key, value, force=force))

    def _set_cookie(self, platform: str, raw: str, force: bool = False) -> str:
        """写入某个平台的 Cookie，返回给用户看的提示。

        「允许不完整，但必须带必备字段」：完整度不检查（平台字段经常变，
        要求填全只会把人挡在门外），但一个必备字段都没有的串肯定是粘错了，
        直接拒收。确实要强行写入时用 ``force``（用户在命令里加「强制」）。
        """
        value = str(raw or "").strip()
        label = self._cookie_label(platform)
        if not value:
            return f"❌ {label} 的 Cookie 是空的，没写入。"

        if not force:
            passed, why = check_cookie(platform, value)
            if not passed:
                return (
                    f"❌ {label} 的 Cookie 没通过检查：{why}\n"
                    f"确认这串没问题的话，改用：`#R配置 cookie {label} 强制 <整串>`"
                )

        err = self._save_conf(_COOKIE_FIELDS[platform], value)
        if err:
            return f"❌ 保存失败：{err}"

        # 「逐项填写」的优先级比整段高，留着旧值会把刚设置的顶掉
        # （B站扫码写凭据时踩过同一个坑）
        cleared = ""
        spec = get_spec(platform)
        if spec is not None:
            existing = self.conf_get(spec.fields_path, []) or []
            if existing:
                err2 = self._save_conf(spec.fields_path, [])
                cleared = (
                    "\n（已顺手清空旧的「逐项填写」，否则它会覆盖这次的值）"
                    if not err2
                    else "\n⚠️ 旧的「逐项填写」没清掉，可能覆盖这次的值，请去 WebUI 检查"
                )

        fields = parse_cookie_keys(value)
        # 强制写入时没有走 `check_cookie`，回执里必须说清楚 ——
        # 否则用户以为「保存成功 = 凭据没问题」，之后用不了会一头雾水。
        tail = (
            "\n⚠️ 这次是**强制写入**（跳过了字段检查）。如果之后用不了，"
            "先怀疑这串凭据本身。"
            if force
            else ""
        )
        return (
            f"✅ {label} 的 Cookie 已保存：{len(value)} 字符 / {len(fields)} 个字段{cleared}\n"
            f"发 `#cookie状态` 可以校验它现在是否有效。{tail}"
        )

    def _clear_cookie(self, platform: str) -> str:
        label = self._cookie_label(platform)
        err = self._save_conf(_COOKIE_FIELDS[platform], "")
        if err:
            return f"❌ 清空失败：{err}"
        spec = get_spec(platform)
        if spec is not None:
            self._save_conf(spec.fields_path, [])
        return f"✅ 已清空 {label} 的 Cookie"

    def _cookie_config_overview(self) -> str:
        lines = ["🔑 Cookie 配置情况", ""]
        filled: list[str] = []
        empty: list[str] = []
        for platform in _COOKIE_FIELDS:
            label = self._cookie_label(platform)
            cookie = self.cookie_for(platform)
            if not cookie:
                empty.append(label)
                continue
            passed, _ = check_cookie(platform, cookie)
            mark = "✅" if passed else "⚠️"
            note = "" if passed else "（缺必备字段）"
            filled.append(
                f"{mark} {label}　{len(cookie)} 字符 / {len(parse_cookie_keys(cookie))} 个字段{note}"
            )
        lines.extend(filled or ["（还没有配置任何 Cookie）"])
        if empty:
            lines.append("")
            lines.append("⬜ 未配置：" + "、".join(empty))
        lines.append("")
        lines.append("设置：#R配置 cookie <平台名>　（私聊发，我提示后再把整串发过来）")
        lines.append("清空：#R配置 cookie <平台名> 清除")
        return "\n".join(lines)

    def _cookie_brief(self) -> str:
        """一行 Cookie 概况（总览用）。"""
        ok: list[str] = []
        warn: list[str] = []
        empty: list[str] = []
        for platform in _COOKIE_FIELDS:
            label = self._cookie_label(platform)
            cookie = self.cookie_for(platform)
            if not cookie:
                empty.append(label)
                continue
            passed, _ = check_cookie(platform, cookie)
            (ok if passed else warn).append(label)

        parts: list[str] = []
        if ok:
            parts.append("✅ " + "、".join(ok))
        if warn:
            parts.append("⚠️ " + "、".join(warn) + "（缺必备字段）")
        if empty:
            parts.append("⬜ " + "、".join(empty))
        return "🔑 Cookie：" + ("　".join(parts) if parts else "（无）")

    def _cookie_label(self, platform: str) -> str:
        """Cookie 平台的中文名（spec 里没写就用本文件的兜底表）。"""
        spec = SPEC_BY_PLATFORM.get(platform)
        return (spec.label if spec and spec.label else "") or _COOKIE_LABELS.get(
            platform, platform
        )

    def has_pending_cookie(self, umo: str) -> bool:
        """该会话是否在等 Cookie（顺手清理过期的）。"""
        entry = self._cookie_pending.get(umo)
        if not entry:
            return False
        deadline, _ = entry
        if time.time() > deadline:
            self._cookie_pending.pop(umo, None)
            return False
        return True

    # ------------------------------------------------------------------
    # 点歌设置
    # ------------------------------------------------------------------

    def _music_label(self, key: str) -> str:
        return self._MUSIC_LABELS.get(key, key)

    def _cfg_music(self, event: AstrMessageEvent, args: list[str]) -> str:
        caps = self._caps(event)
        if not args:
            on = "开" if self.conf_get("music.enable", False) else "关"
            platform = self._music_label(str(self.conf_get("music.platform", "netease") or "netease"))
            count = self.conf_get("music.maxList", 10)
            # 这两项按协议端读：官机那份默认是「语音」
            send = str(self.conf_plat(event, "music_send_mode", "link") or "link")
            search = str(self.conf_plat(event, "music_search_mode", "list") or "list")
            return "\n".join([
                "🎵 点歌设置",
                "",
                f"（当前协议端：{caps.label}）",
                f"点歌开关：{on}",
                f"默认平台：{platform}",
                f"列表长度：{count} 首",
                f"发送方式：{send}（link/card/voice）"
                + ("　⚠️ 本协议端不支持音乐卡片" if not caps.music_card else ""),
                f"点歌方式：{'列表（发列表图，回序号播放）' if search == 'list' else '直接送（直接发第一首）'}",
                "",
                "改：#R配置 点歌 平台|数量|发送|方式|开关 <值>",
                "（发送方式与点歌方式**只改当前协议端这份**）",
            ])

        if len(args) < 2:
            return (
                "❓ 用法：\n"
                "　#R配置 点歌 平台 网易云｜QQ音乐\n"
                "　#R配置 点歌 数量 <1-50>\n"
                "　#R配置 点歌 发送 链接｜卡片｜语音｜文件\n"
                "　#R配置 点歌 方式 列表｜直接\n"
                "　#R配置 点歌 开关 开｜关"
            )

        field_key = args[0].lower()
        raw = "".join(args[1:]).strip()
        word = raw.lower()

        # ---- 默认平台 ----
        if field_key in ("平台", "默认平台", "platform"):
            key = self._MUSIC_PLATFORM_ALIAS.get(word)
            if not key:
                return f"❓ 只支持 网易云 或 QQ音乐，收到「{raw}」。"
            err = self._save_conf("music.platform", key)
            if err:
                return f"❌ 保存失败：{err}"
            return f"✅ 默认点歌平台已改为 **{self._music_label(key)}**"

        # ---- 列表长度 ----
        if field_key in ("数量", "个数", "长度", "maxlist", "count"):
            try:
                value = int(word)
            except ValueError:
                return f"❓ 「{raw}」不是数字。"
            if not 1 <= value <= 50:
                return "❓ 数量要在 1-50 之间（太大了列表图会很长）。"
            err = self._save_conf("music.maxList", value)
            if err:
                return f"❌ 保存失败：{err}"
            return f"✅ 点歌列表长度已改为 **{value}** 首"

        # ---- 发送方式 ----
        if field_key in ("发送", "发送方式", "sendmode"):
            value = self._MUSIC_SEND_ALIAS.get(word)
            if not value:
                return f"❓ 发送方式只认 链接｜卡片｜语音｜文件，收到「{raw}」。"
            err = self._save_conf("music.sendMode", value, event)
            if err:
                return f"❌ 保存失败：{err}"
            extra = ""
            if value == "card" and not caps.music_card:
                extra = f"\n⚠️ {caps.label} 没有音乐卡片消息段，实际会降级成语音。"
            elif value == "card" and not self._sign_proxy_enabled():
                extra = "\n⚠️ 签名代理是关的，音乐卡片不会显示，建议先打开它。"
            return (
                f"✅ 点歌发送方式已改为 **{value}**{extra}"
                f"\n（只影响 {caps.label} 这份配置）"
            )

        # ---- 列表 / 直接送 ----
        if field_key in ("方式", "模式", "searchmode"):
            value = self._MUSIC_SEARCH_ALIAS.get(word)
            if not value:
                return f"❓ 点歌方式只认 列表｜直接，收到「{raw}」。"
            err = self._save_conf("music.searchMode", value, event)
            if err:
                return f"❌ 保存失败：{err}"
            return (
                f"✅ 点歌方式已改为 **{value}**"
                f"（{'发列表图后回序号' if value == 'list' else '直接发第一首'}）"
                f"\n（只影响 {caps.label} 这份配置）"
            )

        # ---- 总开关 ----
        if field_key in ("开关", "enable"):
            if word in self._ON_WORDS:
                on = True
            elif word in self._OFF_WORDS:
                on = False
            else:
                return f"❓ 只认「开」或「关」，收到「{raw}」。"
            err = self._save_conf("music.enable", on)
            if err:
                return f"❌ 保存失败：{err}"
            return f"✅ 点歌已{'开启' if on else '关闭'}"

        return f"❓ 不认识设置项「{args[0]}」，发「#R配置 帮助」看用法。"

    # ------------------------------------------------------------------
    # 发送形式
    # ------------------------------------------------------------------

    def _cfg_form(self, event: AstrMessageEvent, args: list[str]) -> str:
        caps = self._caps(event)
        now = "聊天记录（合并转发）" if self._forward_enabled(event) else "直发（多条消息）"
        # 这条命令改的是**当前协议端**那份配置（见 core/platform_profiles.py）
        scope = (
            "⚠️ 当前协议端不支持合并转发，只能直发。"
            if not caps.forward
            else f"（只影响 {caps.label} 这份配置）"
        )

        if not args:
            return "\n".join([
                f"📤 解析内容发送形式：**{now}**",
                scope,
                "",
                "· 聊天记录：每次解析打包成一条合并转发，简介和全部图片/视频都在里面，",
                "　不再单独发「识别成功」那条提示 —— 内容多的时候不刷屏。",
                "· 直发：简介一条、媒体一条条发 —— 想让内容直接可见时用这个。",
                "",
                "切换：#R配置 形式 聊天记录｜直发",
            ])

        if not caps.forward:
            return (
                f"⚠️ {caps.label} 没有合并转发消息段，只能直发。\n"
                "想在官机上收拢内容，用「#R平台」看它支持什么。"
            )

        word = "".join(args).lower()
        if word in ("聊天记录", "聊天", "转发", "合并转发", "forward", "chat"):
            err = self._save_conf("plugin.send_as_forward", True, event)
            if err:
                return f"❌ 保存失败：{err}"
            return "✅ 已改为 **聊天记录（合并转发）**，下一条链接就按这个形式发。"

        if word in ("直发", "普通", "分开", "direct", "normal", "off"):
            err = self._save_conf("plugin.send_as_forward", False, event)
            if err:
                return f"❌ 保存失败：{err}"
            return "✅ 已改回 **直发（多条消息）**。"

        return f"❓ 只认「聊天记录」或「直发」，收到「{''.join(args)}」。"

    # ------------------------------------------------------------------
    # 写配置
    # ------------------------------------------------------------------

    def _save_conf(
        self, path: str, value, event: AstrMessageEvent | None = None
    ) -> str:
        """把 ``a.b.c`` 路径上的值写进插件配置并落盘，返回错误说明（空串=成功）。

        ⚠️ **路径对应的键必须已经写在 ``_conf_schema.json`` 里**：AstrBot 在
        插件代码跑起来之前会按 schema 裁剪配置，schema 里没有的键写进去当时
        有效、下次启动就被删掉（v1.3.0 的 Cookie 就是这么丢的）。这里只负责写，
        不负责补 schema —— 新增可写配置时记得两边一起加。

        **传了 ``event`` 时会按协议端重定向**：像 ``plugin.send_as_forward``
        这种属于「发送形态」的路径，在分协议端模式下要写到
        ``profiles.<协议端>.<字段>``。否则用户在官机里发
        ``#R配置 形式 直发``，改的却是 OneBot 那份 —— 表现为「命令回了成功，
        但行为没变」，非常难查。
        """
        if event is not None:
            path = profile_save_path(
                self._platform_profiles(), self._caps_key(event), path
            )

        parts = [p for p in str(path).split(".") if p]
        if not parts:
            return "配置路径为空"

        node = self.conf_data
        for part in parts[:-1]:
            nxt = node.get(part) if isinstance(node, dict) else None
            if not isinstance(nxt, dict):
                try:
                    nxt = {}
                    node[part] = nxt
                except (TypeError, KeyError) as exc:
                    return f"配置分组 {part} 不可写（{exc}）"
            node = nxt

        try:
            node[parts[-1]] = value
        except (TypeError, KeyError) as exc:
            return f"写入 {path} 失败（{exc}）"

        saver = getattr(self.conf_data, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[R插件][R配置] 保存配置文件失败: {exc}")
                return f"保存到文件失败（{exc}）"

        logger.info(f"[R插件][R配置] {path} 已更新")
        return ""

    # ==================================================================
    # 点歌搜索
    # ==================================================================

    # 命令里显式写的平台前缀 -> 内部平台 key。
    # **键一律小写**，匹配时把用户输入 `.lower()` 再查（这样 ``Qq``/``QQ``
    # ``qq`` 都能认）。不写就按配置的 music.platform 走。
    _MUSIC_PREFIX: dict[str, str] = {
        "网易云": "netease",
        "网抑云": "netease",
        "网易": "netease",
        "qq音乐": "qqmusic",
        "qq": "qqmusic",
    }

    # 各发送模式一次送出多少首。
    # link 是列表形式（全部列出让人挑）；card / voice / file 每条都是独立
    # 消息，**多发就是刷屏**，所以都只发一首。
    _MUSIC_SEND_LIMIT: dict[str, int] = {
        "link": 0,   # 0 = 不限
        "card": 1,
        "voice": 1,
        "file": 1,
    }

    # 搜索点播会话有效期（秒）。搜索后在这段时间内回复序号即播放对应歌曲。
    _MUSIC_PICK_TTL = 60.0

    # 列表图里最多列几首（配置上限 20，但图上 10 首已经很满）
    _MUSIC_LIST_IMAGE_MAX = 10

    # 语音模式的安全上限（秒）。
    #
    # ⚠️ ``Comp.Record`` 的 ``convert_to_base64()`` 把 ``target_format="wav"``
    # **写死了**（AstrBot 的 ``core/message/components.py``），也就是任何音频进来
    # 都会先转成未压缩 WAV：44100Hz / 16bit / 单声道 ≈ 88.2KB/秒，
    # base64 之后还要再涨 1/3 → 约 117KB/秒，而且这个转换绕不过去。
    # 一首 4 分钟的歌 ≈ 28MB payload，协议端基本收不下。
    # 所以按估算体积提前拦住并明确降级，而不是发出去卡死。
    _MUSIC_VOICE_MAX_SECONDS = 90        # 90 秒 ≈ 10.5MB payload
    _MUSIC_VOICE_BYTES_PER_SEC = 117_000  # base64 后的近似字符数/秒
    # 官机的上限能放宽很多：它**不走跨容器的 base64 HTTP**（AstrBot 内部处理完
    # 直连 QQ 接口），而且适配器会先把音频转成 **silk** 再上传（体积约 1/30），
    # 所以几分钟的歌也能整条发成语音，不用退化成链接。
    _MUSIC_VOICE_MAX_SECONDS_QQ = 300

    def _music_target_platform(self) -> str:
        """配置里选的默认点歌平台。"""
        raw = str(self.conf_get("music.platform", "netease") or "netease")
        return {"netease": "netease", "qq": "qqmusic", "qqmusic": "qqmusic"}.get(
            raw, "netease"
        )

    def _music_search_mode(self, event: AstrMessageEvent | None = None) -> str:
        """搜索后的呈现方式。

        - ``list``（默认）：发列表图，60 秒内回序号点播 —— 「搜索点歌」
        - ``direct``：直接按 ``sendMode`` 送出第一首 —— 保留原来的「指定点歌」
        """
        raw = str(
            self.conf_plat(event, "music_search_mode", "list") or "list"
        ).strip().lower()
        return raw if raw in ("list", "direct") else "list"

    # ------------------------------------------------------------------
    # 序号点播会话
    # ------------------------------------------------------------------

    def _remember_music_pick(
        self, event: AstrMessageEvent, keyword: str, used: str, label: str, songs: list
    ) -> None:
        """记下这次的候选列表，供 60 秒内按序号点播。"""
        now = time.time()
        # 顺手清掉过期会话，避免长期堆积（会话量 = 活跃群数，很小）
        for key in [
            k for k, v in self._music_sessions.items()
            if now - v[0] > self._MUSIC_PICK_TTL
        ]:
            self._music_sessions.pop(key, None)
        self._music_sessions[event.unified_msg_origin] = (
            now, keyword, label, used, list(songs),
        )

    def _take_music_pick(self, event: AstrMessageEvent):
        """**只读**该会话的候选列表（不消费）；没有或已过期返回 None。

        真正「用掉」会话要显式调 ``_forget_music_pick()``。分成两步是为了
        区分两种情况：

        - **序号越界**：什么都没播，会话要留着让用户重试
        - **点播成功**：立刻清掉，否则同一个序号可以无限重发（真正踩过的 bug）

        过期会话顺手清掉，避免长期堆积（会话量 = 活跃群数，很小）。
        """
        item = self._music_sessions.get(event.unified_msg_origin)
        if not item:
            return None
        if time.time() - item[0] > self._MUSIC_PICK_TTL:
            self._music_sessions.pop(event.unified_msg_origin, None)
            return None
        return item

    def _forget_music_pick(self, event: AstrMessageEvent) -> None:
        """清掉该会话的候选列表。

        ⚠️ 必须在**决定要播出之后、任何 ``await`` 之前**同步调用。
        asyncio 是单线程事件循环，这段没有让出点，所以「连点两次序号」
        产生的两个事件里，第二个拿到的必然已是空会话 —— 不会重复发歌。
        """
        self._music_sessions.pop(event.unified_msg_origin, None)

    def _music_send_mode(self, event: AstrMessageEvent | None = None) -> str:
        """配置里选的发送方式，并**按协议端能力兜底**。

        两道兜底，顺序不能反：

        1. 认不出的值（改配置手滑）→ 最稳的 ``link``；
        2. **配置选的能力本协议端没有** → 降级。最典型的是 ``card``
           （音乐卡片）在 QQ 官方机器人上不存在 —— 那边只有 ``msg_type``
           里没有 music 段，硬发就是整条失败。这时降级成 **``voice``**
           （用户要的「用语音代替音乐卡片」）。

        ⭐ 为什么能力兜底放这么靠前：**偏好不能盖过能力**。
        ``conf_plat`` 有一条「面板还是默认值时继承旧键」的兼容规则，
        所以用户以前设的 ``music.sendMode=card`` 会被带到官机那份配置里；
        没有这道兜底，官机就会拿着 ``card`` 去发一个它做不到的形态。
        """
        mode = str(
            self.conf_plat(event, "music_send_mode", "link") or "link"
        ).strip().lower()
        if mode not in self._MUSIC_SEND_LIMIT:
            return "link"

        if mode == "card" and event is not None and not self._caps(event).music_card:
            logger.debug(
                f"[R插件] {self._caps(event).label} 没有音乐卡片消息段，"
                "点歌发送方式降级为语音"
            )
            return "voice"
        return mode

    async def _music_resolve_url(self, song, *, high: bool = True) -> str:
        """取音频直链（自动带上配置里对应平台的 Cookie）。

        ``high=False`` 走**标准音质** —— 官机发语音时用这个：适配器最终会把音频
        转成 **silk** 再上传，高音质白白多花几倍下载时间，还更容易失败。
        """
        try:
            return await music_get_play_url(
                song,
                netease_cookie=self.cookie_for("netease"),
                qq_cookie=self.cookie_for("qqmusic"),
                high=high,
            )
        except Exception as exc:  # noqa: BLE001 - 取直链失败不该让整条命令崩
            logger.warning(f"[R插件] 点歌取直链失败: {type(exc).__name__}: {exc}")
            return ""

    @staticmethod
    def _music_fail_reason(platform: str) -> str:
        """取直链失败时**按平台说清原因**，别丢一句「可能没配 Cookie」。

        两个平台的失败原因完全不同（都是实测结论）：

        * **QQ音乐**：``result=104003`` ＝ **登录态过期**。``qqmusic_key``
          只有约 12 小时有效期，过期后搜索照常、但取直链恒失败 ——
          含糊的提示会让人以为插件坏了，然后反复重试。
        * **网易云**：直链为空一般是 ``fee=1/4``（VIP / 需购买），
          光有 Cookie 也不够，换版本比换 Cookie 有用。
        """
        if platform == "qqmusic":
            return (
                "QQ音乐的登录态过期了（`qqmusic_key` 约 12 小时有效期，"
                "过期后搜索正常但取不到播放地址）。\n"
                "重新抓一次 Cookie 发给我就能恢复：`#R配置 cookie QQ音乐`"
            )
        if platform == "netease":
            return "这首歌可能需要会员（或已下架），换个版本试试"
        return "音频地址取不到"

    @staticmethod
    def _music_link(song) -> str:
        """降级提示里给出的链接：**优先音频直链**，没有才退回歌曲详情页。

        用户要求（2026-09-23）：降级时给「文件直链」而不是详情页 ——
        直链在客户端能**直接点开播放**，详情页还要再跳一次。
        ``play_url`` 就是「语音发送」用的同一条地址，而且走到这些降级分支时
        它通常**已经验证过可播**（``music_verify_audio`` 过了才会去下载）。

        ⚠️ ``play_url`` 带签名（``x-expires``）**有时效** —— 过期后点开会失效。
        所以文案里不要写「过一会儿再点一次就好」（那是给详情页的话），
        要写「失效了重新点一次歌」。

        ``_music_render_link``（link 模式）和「歌曲详情」按钮**不使用**这个函数：
        那两个的语义本来就是「给页面」。
        """
        play = str(getattr(song, "play_url", "") or "").strip()
        return play or str(getattr(song, "page_url", "") or "")

    def _music_fail_hint(self, song) -> str:
        return (
            f"🎵 {self._music_fail_reason(song.platform)}\n\n"
            f"先给链接：\n{self._music_link(song)}"
        )

    @staticmethod
    def _music_card(song) -> Comp.Music | None:
        """把一首歌构造成音乐分享卡。**统一走 ``custom``**。

        ⚠️ **为什么只能是 custom（都是实测结论，别再改回去）**

        OneBot 的 ``music`` 段在 NapCat/SnowLuma 里**不自己生成卡片**：它把请求
        POST 给外部「音卡签名服务」，再把返回的 JSON 原样当 lightApp 发出去。
        而 QQ 对 lightApp 有强校验 —— ``config.token`` 无效就回
        「发送者版本过低，无法展示内容」（这是**通用的验证失败提示**，
        不是字面意义的版本问题，见 Lagrange.Core 的相关分析）。

        实测两个关键事实：

        1. **id 模式（``type=163`` / ``type=qq`` + ``id``）已被签名服务弃用**：
           官方 NapCat 首选签名服务 ``http://106.55.0.102:10087/`` 对 id 模式
           直接返 HTTP 400「缺少 title」；旧的 ``ss.xingzhige.com`` 则返回纯文本
           「关闭id解析功能」（不是 JSON）。
        2. **官方 NapCat 的校验要求 ``url`` / ``audio`` / ``title`` / ``image``
           全部非空**（``packages/napcat-onebot/api/msg.ts`` 逐个校验，
           缺一个就 ``return undefined`` —— 整条消息静默丢弃）。

        ``_type`` 必须用 ``object.__setattr__`` 写入：它不在 pydantic 模型字段里，
        构造时传会被**静默忽略**、实例化后直接赋值会被 ``__setattr__`` 拦下报
        ``ValueError``；而 AstrBot 的 respond stage 有校验器读它，
        缺了会抛 ``AttributeError`` 导致整条消息发不出去。

        为什么这里所有平台都塞 ``custom``
        ---------------------------------

        AstrBot 的 Music 校验器是::

            (comp.id and comp._type and comp._type != "custom")
            or (comp._type == "custom" and comp.url and comp.audio and comp.title)

        非 custom 分支**要求 ``id`` 存在**，而 id 模式已被签名服务停用，
        所以我们只能走 custom 分支。**卡片最终显示哪个平台，由我们的签名代理
        （``core/music_sign_proxy.py``）在请求侧按歌曲页域名改 ``type`` 决定** ——
        那一步发生在签名**之前**，所以 token 依然匹配。

        返回 ``None`` 表示这首歌构造不出合法卡片（调用方应跳过）。
        """
        if not (song.name and song.page_url):
            return None
        # image 是官方强校验项，缺了整条消息会被丢弃 —— 没有封面就退占位图
        # （QQ 互联的音乐图标，稳定可达）
        image = song.cover or "https://p.qpic.cn/qqconnect/0/app_100497308_1626060999/100"
        comp = Comp.Music(
            url=song.page_url,
            # audio 官方必填；直链取不到时退化成页面地址，至少让卡片能发出去
            audio=song.play_url or song.page_url,
            title=song.name,
            content=song.artist,
            image=image,
        )
        object.__setattr__(comp, "_type", "custom")
        return comp

    async def cmd_music_search(self, event: AstrMessageEvent):
        """``点歌 <关键词>`` —— 搜索歌曲并按配置的发送方式送出。

        **指令格式**（正则与 ``COMMAND_RULES`` 共用一份，见
        ``core/constants.py::MUSIC_COMMAND_PATTERN``）：

        ==========================  ==============================
        输入                         行为
        ==========================  ==============================
        ``点歌 晴天``                用配置里的「默认平台」
        ``网易云点歌 晴天``           强制网易云
        ``QQ点歌 晴天``              强制 QQ 音乐
        ``#点歌 网易云 晴天``         平台写在后面也认
        ==========================  ==============================

        **平台选择**：命令里写了平台（无论前后）就用它，**不会**偷换成别的；
        没写才用配置的「默认平台」，且**该平台搜不到时自动试另一个**
        （群里体验优先，不然用户会以为点歌坏了）。

        **发送方式**由 ``music.sendMode`` 决定：

        - ``link``：列出「歌名 - 歌手 + 播放页链接」（最稳，零额外请求）
        - ``card``：发音乐分享卡（QQ 音乐走官方 ``qq`` 卡片，网易云走 ``163``）
        - ``voice``：下载音频后以语音条发出（受 WAV 体积限制，见
          ``_MUSIC_VOICE_MAX_SECONDS``）
        - ``file``：以群文件形式发音频（``Comp.File`` + URL，协议端自己去拉）
        """
        if not self.conf_get("music.enable", False):
            yield event.plain_result(
                "🎵 点歌功能未启用。\n"
                "请在插件配置的「点歌」分组里打开 **开启点歌**。"
            )
            return

        text = event.get_message_str().strip()
        if "点歌" not in text:
            return
        m = re.search(MUSIC_COMMAND_PATTERN, text, re.IGNORECASE | re.MULTILINE)
        if not m:
            return

        # 平台写在「点歌」前面或后面都认，**前面优先**
        platform_raw = (m.group("pre") or m.group("post") or "").strip()
        platform_key = self._MUSIC_PREFIX.get(platform_raw.lower())
        keyword = (m.group("kw") or "").strip()
        if not keyword:
            yield event.plain_result(
                "🎵 用法：`点歌 歌名`\n"
                "指定平台：`网易云点歌 歌名` / `QQ点歌 歌名`"
            )
            return

        limit = int(self.conf_get("music.maxList", 10) or 10)
        limit = max(1, min(limit, 20))

        # 搜索。用户在命令里明确指定平台时不 fallback——尊重用户的选择，
        # 也避免"我要网易云却给我 QQ音乐"的意外。
        songs, used = await music_search(keyword, platform=platform_key, limit=limit)

        if not songs and platform_key is None:
            fallback = (
                "qqmusic" if self._music_target_platform() == "netease" else "netease"
            )
            logger.info(f"[R插件] 点歌「{keyword}」在默认平台无结果，回退到 {fallback}")
            songs, used = await music_search(keyword, platform=fallback, limit=limit)

        if not songs:
            # QQ 音乐接口有随机限流（见 core/music_search.py 的说明），
            # 重试仍失败是正常现象，提示用户重试而不是说"搜不到"。
            if platform_key == "qqmusic" or (
                platform_key is None and self._music_target_platform() == "qqmusic"
            ):
                yield event.plain_result(
                    f"🎵 QQ音乐接口忙，没搜到「{keyword}」。\n"
                    "这是接口的随机限流（重试几次通常会好），可以：\n"
                    "· 稍后再试一次\n"
                    f"· 换网易云：`网易云点歌 {keyword}`"
                )
            else:
                yield event.plain_result(
                    f"🎵 没搜到「{keyword}」。\n"
                    "换个关键词试试，或指定平台：`QQ点歌 歌名`"
                )
            return

        label = PLATFORM_LABELS.get(used, used)
        caps = self._caps(event)
        mode = self._music_send_mode(event)
        if mode == "card" and not caps.music_card:
            # 官方机器人**没有 Comp.Music 这个段**，卡片必定发不出去 ——
            # 按「音乐卡片改用语音代替」自动降级，而不是让用户空等一条失败。
            logger.info("[R插件] 本协议端不支持音乐卡片，发送方式自动降级为语音")
            mode = "voice"
        logger.info(f"[R插件] 点歌「{keyword}」-> {label} {len(songs)} 首，发送方式={mode}")

        # 「搜索点歌」（默认）：发一张列表图，并记下候选列表，
        # 用户 60 秒内回序号即点播 —— 与「指定点歌」（searchMode=direct，直接送）
        # 并存，由配置切换。
        if self._music_search_mode(event) == "list":
            async for item in self._music_render_list_image(
                event, songs, used, label, keyword
            ):
                yield item
            return

        if mode == "link":
            async for item in self._music_render_link(event, songs, used, label, keyword):
                yield item
            return

        # card / voice / file 每条都是独立消息，按上限取前 N 首
        per = self._MUSIC_SEND_LIMIT[mode]
        targets = songs if per == 0 else songs[:per]

        # 只有 voice / file 需要「正在准备」提示（它们要下载音频，有等待感）。
        # card 直接发卡片——卡片本身带歌名歌手，再补一条文字纯属重复刷屏。
        if mode in ("voice", "file") and targets:
            yield event.plain_result(
                f"🎵 {label} · 点歌「{keyword}」\n正在准备「{targets[0].label}」…"
            )

        sent = 0
        for song in targets:
            if mode == "card":
                # custom 卡片的 audio 是官方强校验项（缺了整个消息段会被丢弃），
                # 所以必须取直链；而且必须是**能播的**直链，否则卡片点不开
                song.play_url = await self._music_resolve_url(song)
                if song.play_url and not await music_verify_audio(song.play_url):
                    logger.info(f"[R插件] 「{song.label}」直链不可用，不构造卡片")
                    song.play_url = ""
                comp = self._music_card(song)
                if comp is None:
                    logger.info(f"[R插件] 点歌「{song.label}」构造不出卡片，跳过")
                    continue
                yield event.chain_result([comp])
                sent += 1
                continue

            song.play_url = await self._music_resolve_url(song)
            if not song.play_url:
                continue
            # 发送前确认链接真是音频 —— 防止把「404 网页」当 mp3 发出去
            if not await music_verify_audio(song.play_url):
                logger.info(f"[R插件] 「{song.label}」直链不可用，跳过（改发链接）")
                continue

            if mode == "voice":
                async for item in self._music_render_voice(event, song, label):
                    yield item
                sent += 1
            else:  # file
                yield event.chain_result(
                    [Comp.File(name=f"{song.label}.mp3", url=song.play_url)]
                )
                sent += 1

        if not sent:
            # 兜底：所有目标都没成功，至少把链接给出去，别让用户空等
            yield event.plain_result(
                f"🎵 {self._music_fail_reason(used)}\n\n先给这几首的链接：\n"
                + "\n".join(f"· {s.label}\n  {self._music_link(s)}" for s in songs[:3])
            )

    async def _send_music_list_md(
        self, event, songs, keyword: str, label: str, used: str
    ) -> bool:
        """（官机）点歌搜索结果：**一条 MD 列表，歌名文字本身可点**。

        ⭐ 用的是 QQ 官方的**内联指令**（``mqqapi://aio/inlinecmd``）：
        每行写成链接样式，**点一下就把序号填进输入框**，用户直接发送即点播 ——
        所以**不需要底部按钮**，列表看起来就是普通文字列表（正是要的效果）。

        没用「回车指令」（``enter=true`` 点了直接发送）的原因：官方注明
        **群聊不支持**那个能力，而群聊恰恰是主要场景，所以统一走参数指令。
        """
        lines = [f"# 🎵 {label} · 点歌「{keyword}」", ""]
        for i, song in enumerate(songs, 1):
            lines.append(qq_inline_cmd(f"{i}. {song.label}", str(i)))
        lines += [
            "",
            f"> 点歌名即可，序号会自动填进输入框"
            f"（{int(self._MUSIC_PICK_TTL)} 秒内有效）",
        ]
        if used == "qqmusic" and not self.cookie_for("qqmusic"):
            lines.append("> ⚠️ 未配 QQ音乐 Cookie，可能只能试听")
        return await self._send_qq_payload(
            event, {"msg_type": 2, "markdown": {"content": "\n".join(lines)}}
        )

    async def _music_render_list_image(
        self, event, songs, used: str, label: str, keyword: str
    ):
        """``searchMode=list``：发点歌列表，并记下候选列表供序号点播。

        * **官机**：列表图是**本地渲染的 PNG**（没有公网 URL、塞不进 markdown，
          而按钮只能挂 markdown）→ 改用「MD 列表 + 每首歌一个按钮」，
          见 ``_send_music_list_md``。
        * **其它协议端**：Pillow 现画一张列表图（无二维码）；画不出来退回文字列表。

        ⚠️ 用 ``Comp.Image.fromBytes`` 而不是 ``fromFileSystem``：
        后者只存路径，真正读文件发生在**发送阶段**的 ``convert_to_base64()``，
        那样临时文件一旦提前清理就会发不出去。
        """
        shown = songs[: self._MUSIC_LIST_IMAGE_MAX]
        self._remember_music_pick(event, keyword, used, label, shown)
        hint = f"回复 1-{len(shown)} 播放（{int(self._MUSIC_PICK_TTL)} 秒内有效）"

        caps = self._caps(event)
        if caps.keyboard and self._qq_buttons_enabled(event):
            if await self._send_music_list_md(event, shown, keyword, label, used):
                return
            logger.info("[R插件] MD 点歌列表发送失败，退回文字列表")

        png = await render_song_list(shown, keyword, label, used, hint)
        if png:
            try:
                yield event.chain_result([Comp.Image.fromBytes(png)])
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 列表图发送失败，退回文字列表: {exc}")

        async for item in self._music_render_link(event, songs, used, label, keyword):
            yield item

    @staticmethod
    def _music_cover_url(cover: str, *, size: int = 500) -> str:
        """把封面 URL 规整成「**https + 合适尺寸**」，供 markdown 内嵌用。

        ⚠️ 两个必须处理的点（2026-09-23 实测）：

        1. **网易云给的是 ``http://``** —— QQ 的 markdown 图片只认 https，
           原样塞进去手机上就是「图片加载失败」；
        2. **尺寸**：网易云给的是原图（实测 118KB ~ 929KB，太大），
           QQ音乐给 ``T002R150x150M``（150×150，当专辑图会糊）。
           两边的 URL 都能改：
           网易云加 ``?param=WyH``、QQ音乐把 ``R150x150`` 换成 ``R{size}x{size}``。

        认不出的域名原样返回（只把 http 升成 https），不改结构。
        """
        if not cover:
            return ""
        url = cover
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if "music.126.net" in url:
            return f"{url.split('?', 1)[0]}?param={size}y{size}"
        if "gtimg.cn" in url:
            return re.sub(r"R\d+x\d+M", f"R{size}x{size}M", url)
        return url

    async def _send_music_detail_md(self, event, song, label: str) -> bool:
        """（官机）点播结果：**一条 MD** —— 专辑图 + 小字歌曲信息 + 两个跳转按钮。

        **专辑图是公网 CDN 的 URL**（网易云 / QQ音乐封面），能直接 markdown 内嵌 ——
        这也是官机上唯一能让「图片 + 按钮」出现在同一条的路子（本地渲染的图没有
        公网 URL，而按钮只能挂 markdown）。

        三处细节：

        * 「小字」：markdown 没有字号控制，用**引用块**（``>``）表达「次要信息」，
          视觉上就是图下面那行浅色小字；
        * **「保存音频」按钮指向音频直链**（``play_url``）—— 用户要求「点开能保存」
          （2026-09-23）。这条直链**刚取出来、刚验证过可播**，所以点开就是音频文件，
          浏览器/客户端能直接下载保存。⚠️ 它带签名（``x-expires``）**有时效**；
        * **「歌曲详情」按钮指向详情页**（``page_url``）—— 永久入口，直链失效了还能
          从那儿找到这首歌。**两个都给**：一个能保存、一个不会过期。

        成功后调用方紧接着**单独**发语音（用户要的就是「MD 图文一条 + 语音一条」）。
        """
        cover = self._music_cover_url(song.cover)
        size = await self._probe_image_size(cover) if cover else None

        lines = [f"# {song.name}"]
        if cover:
            lines += [
                "",
                self._md_image(
                    cover, size, alt="专辑图", max_width=self._md_image_width(event)
                ),
            ]
        meta: list[str] = []
        if song.artist:
            meta.append(song.artist)
        if song.album:
            meta.append(f"《{song.album}》")
        if song.duration:
            meta.append(f"{song.duration // 60}:{song.duration % 60:02d}")
        if meta:
            lines += ["", "> " + " · ".join(meta)]
        if label:
            lines.append(f"> 来自 {label}")
        md = "\n".join(lines)

        buttons: list[list] = []
        if song.page_url:
            buttons.append([qq_link_button("歌曲详情", song.page_url, style=1)])
        play = str(getattr(song, "play_url", "") or "").strip()
        if play:
            # 直链 —— 点开就是音频文件，能存下来
            buttons.append([qq_link_button("保存音频", play, style=1)])
        if buttons:
            return await self._send_md_with_buttons(event, md, buttons)
        return await self._send_qq_payload(
            event, {"msg_type": 2, "markdown": {"content": md}}
        )

    async def cmd_music_pick(self, event: AstrMessageEvent):
        """``<序号>`` —— 点歌列表发出后 60 秒内回数字即播放对应歌曲。

        **只在存在有效会话时响应**：没有会话直接返回，不干扰群里的普通数字
        消息（也不会 stop_event，其他处理器照常工作）。

        **序号是一次性的**：点播成功后立刻清掉会话，再发同一个序号就静默
        无响应（要再点播得重新搜一次）。序号越界**不**清会话，方便重试。
        """
        if not self.conf_get("music.enable", False):
            return
        if self._music_search_mode(event) != "list":
            return

        text = event.get_message_str().strip()
        if not text.isdigit():
            return
        index = int(text)
        session = self._take_music_pick(event)
        if session is None:
            return

        _ts, keyword, label, used, songs = session
        if not 1 <= index <= len(songs):
            yield event.plain_result(
                f"🎵 序号要在 1-{len(songs)} 之间哦（本次点歌「{keyword}」）"
            )
            return

        song = songs[index - 1]
        # 立刻消费掉会话：序号是**一次性**的，再发一次就静默无响应。
        # 必须在这里（任何 await 之前）同步清 —— 否则同一个序号能无限重发，
        # 而且连点两次会各播一遍。
        self._forget_music_pick(event)
        logger.info(f"[R插件] 序号点播「{keyword}」#{index} -> {song.label}")

        # 官机：一条 **MD 图文**（专辑图 + 小字信息 + 「歌曲详情」跳转按钮），
        # 再**单独**一条语音 —— 用户要的就是这个形态。
        caps = self._caps(event)
        if caps.keyboard and self._qq_buttons_enabled(event):
            # 标准音质就够：语音最终会被适配器转成 silk，高音质只是白等下载
            song.play_url = await self._music_resolve_url(song, high=False)
            if not song.play_url:
                yield event.plain_result(self._music_fail_hint(song))
                return

            # **下载和发 MD 并行** —— 两者互不依赖，串起来等于白等一次 2~5 秒
            # 的下载。这边先把音频下着，MD 一发出就直接接语音。
            # （不额外调 music_verify_audio：下载失败本身就会抛出来。）
            dl_task = asyncio.create_task(
                download_media(song.play_url, prefix="music", timeout=90.0)
            )
            md_ok = await self._send_music_detail_md(event, song, label)
            audio_path = None
            try:
                audio_path = await dl_task
            except (HttpError, MediaTooLarge) as exc:
                logger.warning(f"[R插件] 点歌音频下载失败: {exc}")

            if md_ok:
                if audio_path is None:
                    yield event.plain_result(
                        f"🎵「{song.label}」音频下载失败，改用链接：\n"
                        f"{self._music_link(song)}"
                    )
                    return
                async for item in self._music_render_voice(
                    event, song, label, preloaded=audio_path
                ):
                    yield item
                return
            logger.info("[R插件] MD 点播详情发送失败，退回原发送方式")

        mode = self._music_send_mode(event)
        if mode == "card" and not caps.music_card:
            # 官方机器人没有 Comp.Music → 卡片必定失败，降级成语音
            logger.info("[R插件] 本协议端不支持音乐卡片，发送方式自动降级为语音")
            mode = "voice"
        if mode == "link":
            async for item in self._music_render_link(event, [song], used, label, keyword):
                yield item
            return

        if mode == "card":
            song.play_url = await self._music_resolve_url(song)
            comp = self._music_card(song)
            if comp is not None:
                yield event.chain_result([comp])
                return
            yield event.plain_result(
                f"🎵 卡片构造失败，先给链接：\n{self._music_link(song)}"
            )
            return

        song.play_url = await self._music_resolve_url(song)
        if not song.play_url:
            yield event.plain_result(self._music_fail_hint(song))
            return

        if mode == "voice":
            async for item in self._music_render_voice(event, song, label):
                yield item
        else:  # file
            yield event.chain_result(
                [Comp.File(name=f"{song.label}.mp3", url=song.play_url)]
            )

    async def _music_render_link(self, event, songs, used: str, label: str, keyword: str):
        """``link`` 模式：列出「歌名 - 歌手 + 播放页链接」。"""
        lines = [f"🎵 {label} · 点歌「{keyword}」", ""]
        for i, song in enumerate(songs, 1):
            lines.append(f"{i}. {song.label}")
            if song.page_url:
                lines.append(f"   {song.page_url}")
        if used == "qqmusic" and not self.cookie_for("qqmusic"):
            lines.append("")
            lines.append("💡 未配置 QQ音乐 Cookie，链接点开可能只能试听")
        yield event.plain_result("\n".join(lines))

    async def _music_to_voice_wav(self, src: Path) -> Path | None:
        """把音频转成「语音条友好」的小 wav —— **16kHz 单声道**。

        **这是语音发送最大的一处提速**（实测 268 秒的歌）：

        ====================  ================  ==============
        格式                  体积               base64 后
        ====================  ================  ==============
        44.1k 立体声 wav      60.3 MB           60.3 MB
        **16k 单声道 wav**    8.6 MB            11.5 MB
        ====================  ================  ==============

        **为什么这样能生效**：AstrBot 的 ``Record.convert_to_base64()`` 写死了
        ``to_base64(target_format="wav")``，看起来躲不掉；但底层的
        ``ensure_wav()`` 有一条：「发现已经是 wav 就**直接返回、跳过转码**」。
        所以插件抢先转成小 wav 之后，框架那一步就从「转码 5.3 秒 / 60MB」
        变成「读文件几十毫秒」。

        语音条最终会被协议端转成 silk（本来就是窄带），16kHz 单声道听感没有损失。
        转码失败返回 ``None``，调用方用原文件（只是慢一点，功能不受影响）。
        """
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            return None
        out = src.with_name(src.stem + ".voice.wav")
        try:
            result = await run(
                ffmpeg, "-y", "-loglevel", "error",
                "-threads", "0",          # 让 ffmpeg 自己决定线程数
                "-i", str(src),
                "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(out),
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件] 语音预转码异常: {type(exc).__name__}: {exc}")
            return None
        out_size = out.stat().st_size if out.exists() else 0
        if result.ok and out_size > 0:
            logger.info(
                f"[R插件] 语音预转码: {src.stat().st_size // 1024}KB -> "
                f"{out_size // 1024}KB（16k 单声道，框架将跳过二次转码）"
            )
            return out
        logger.debug("[R插件] 语音预转码失败（ffmpeg 非 0），退回原文件")
        return None

    async def _music_render_voice(
        self, event, song, label: str, *, preloaded=None
    ):
        """``voice`` 模式：下载音频后以语音条发送。

        ⚠️ 体积问题：``Comp.Record`` 会把音频转成未压缩 WAV，base64 后
        约 117KB/秒的 payload。**同容器部署**下这只是一个较大的 HTTP 请求；
        **跨容器**时要在 HTTP body 里塞这么多字符，长歌大概率失败。
        所以先用 ``song.duration`` 估算，超限就明确降级到链接，
        而不是发出去让用户等到超时。

        ``preloaded``：调用方**已经并行下好**的音频路径。传了就不再下载 ——
        官机的「MD 详情 + 语音」用它把下载和发 MD 重叠起来，省掉一次串行等待。
        """
        caps = self._caps(event)
        limit = (
            self._MUSIC_VOICE_MAX_SECONDS_QQ
            if caps.key == "qqofficial"
            else self._MUSIC_VOICE_MAX_SECONDS
        )
        est = song.duration * self._MUSIC_VOICE_BYTES_PER_SEC
        if song.duration > limit:
            tip = (
                "（官方机器人发不了音乐卡片，长歌只能给链接）"
                if caps.key == "qqofficial"
                else "（想要整首可听，把「发送方式」改成 **音乐卡片** 或 **音频文件**）"
            )
            yield event.plain_result(
                f"🎵「{song.label}」约 {song.duration // 60} 分 {song.duration % 60} 秒，"
                f"语音条发不下（WAV 体积约 {est / 1024 / 1024:.0f}MB）。\n"
                f"改用链接：\n{self._music_link(song)}\n{tip}"
            )
            return

        if preloaded is not None:
            path = preloaded
        else:
            try:
                path = await download_media(
                    song.play_url, prefix="music", timeout=90.0
                )
            except (HttpError, MediaTooLarge) as exc:
                logger.warning(f"[R插件] 点歌音频下载失败: {exc}")
                yield event.plain_result(
                    f"🎵「{song.label}」音频下载失败，改用链接：\n"
                    f"{self._music_link(song)}"
                )
                return

        # 抢先转成 16kHz 单声道 wav：框架认「已是 wav」就会跳过它自己那次
        # 5 秒级、60MB 的转码（详见 _music_to_voice_wav 的实测表）。
        small = await self._music_to_voice_wav(path)
        if small is not None:
            try:
                path.unlink()  # 原文件不再需要
            except OSError:
                pass
            path = small

        # 登记给 AstrBot，事件结束后自动回收
        try:
            event.track_temporary_local_file(str(path))
        except Exception:  # noqa: BLE001 - 老版本可能没这个方法
            pass

        # 官机：自己转 silk + 自己上传发送。走适配器的话，腾讯上传接口一抖就要
        # 重试 3 次、用户干等 90 秒才被告知失败（详见 _send_qq_voice 的说明）。
        # 预转失败（small is None）时不走这条 —— 非 16k 单声道的输入不在
        # encode_silk 的处理范围内，硬走只会白折腾。
        if caps.key == "qqofficial" and small is not None and qq_silk_available():
            if await self._send_qq_voice(event, path):
                event.stop_event()
                return
            yield event.plain_result(
                f"🎵「{song.label}」语音上传超时（QQ 接口不稳），先给链接：\n"
                f"{self._music_link(song)}\n"
                "（链接带时效，失效了重新点一次歌就行）"
            )
            event.stop_event()
            return

        try:
            comp = Comp.Record.fromFileSystem(str(path))
            yield event.chain_result([comp])
        except Exception as exc:  # noqa: BLE001 - 转码失败时别静默
            logger.warning(f"[R插件] 点歌语音转码失败: {type(exc).__name__}: {exc}")
            yield event.plain_result(
                f"🎵「{song.label}」语音转码失败（音频可能过长），改用链接：\n"
                f"{self._music_link(song)}"
            )
        event.stop_event()

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

        # 关停音乐签名代理（释放监听端口）
        if self._sign_proxy is not None:
            await self._sign_proxy.stop()
            self._sign_proxy = None

        # 关停常驻的 node 签名进程（不留僵尸进程）
        await close_a_bogus_worker()

        # 关闭共享 HTTP 连接池，释放 socket
        await close_http_session()

        logger.info("[R插件] 已卸载")
