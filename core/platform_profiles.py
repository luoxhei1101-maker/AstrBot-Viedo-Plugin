"""分协议端配置档：每个机器人一套自己的「发送形态」。

**为什么要有这一层**

插件同时服务 OneBot v11 和 QQ 官方机器人，而这两边「能怎么发」差别很大：

* OneBot 能发合并转发（聊天记录）、能发音乐卡片；
* QQ 官方机器人两样都没有，但能发原生 markdown + 消息按钮。

把偏好和物理能力混在一个面板里，结果就是**每个平台都要忍受另一边的选项** ——
官机上「用聊天记录」永远发不出去、「发送方式=音乐卡片」永远降级，
用户只会以为插件坏了。

所以配置分两处：

* ``plugin.*`` / ``music.*`` —— **原位置**，现在语义是「通用配置」，
  只在 ``profiles.mode = shared``（配置来源 = 通用）时生效。
* ``profiles.onebot`` / ``profiles.qqofficial`` / ``profiles.fallback``
  —— **每个协议端一份**，默认（``per_platform``）就走这里。

**默认值的选取原则：等于「当前实际生效的行为」**，这样升级到本版
不会让任何人的行为发生变化 —— 只是把原本隐含在各处 ``if 官方机器人``
里的差异，变成配置面板上看得见、改得动的值。

⚠️ **``profiles`` 里的键必须同时写进 ``_conf_schema.json``**：AstrBot 在
插件代码跑起来之前会按 schema 裁剪配置，schema 里没有的键会被删掉
（v1.3.0 的 Cookie 就是这么丢的）。``tests/test_platform_profiles.py``
有一条断言把「代码字段」和「schema 键」锁死，防止以后加字段只改一边。

**关于「选项卡」**：AstrBot 的 schema 只支持
``int / float / bool / string / text / list / file / object / template_list / dict``，
**没有 tabs 控件**；但 ``object`` 可以嵌套，前端会渲染成**可折叠分组** ——
所以这里用「一个协议端一个嵌套 object」当作选项卡的等价形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# --------------------------------------------------------------------------
# 配置来源
# --------------------------------------------------------------------------

#: 每个协议端各读自己那份（默认）
MODE_PER_PLATFORM = "per_platform"
#: 全部跟随 ``plugin.*`` / ``music.*`` 里的旧值（升级后想保持老行为就选它）
MODE_SHARED = "shared"

MODE_OPTIONS = (MODE_PER_PLATFORM, MODE_SHARED)

# --------------------------------------------------------------------------
# 协议端分组
# --------------------------------------------------------------------------

ONEBOT = "onebot"
QQOFFICIAL = "qqofficial"
FALLBACK = "fallback"

GROUP_LABELS: dict[str, str] = {
    ONEBOT: "OneBot v11（NapCat / Lagrange / go-cqhttp）",
    QQOFFICIAL: "QQ 官方机器人（botpy）",
    FALLBACK: "其它协议端（微信 / 钉钉 / Telegram…）",
}


def group_for(caps_key: str | None) -> str:
    """协议端 key → 配置分组名。

    ``caps_key`` 一般来自 ``core/platform_caps.platform_key()``（它已经把小写 /
    下划线 / 连字符的写法归一过了），但这里**自己再认一遍**：直接传原始平台名
    （``qq_official`` / ``QQ-Official``）进来的调用方不应该拿到错的分组 ——
    分错组意味着读到另一份配置，表现是「改了没反应」。
    """
    key = str(caps_key or "").strip().lower()
    for hint in ("qqofficial", "qq_official", "qq-official"):
        if hint in key:
            return QQOFFICIAL
    if key == "aiocqhttp":
        return ONEBOT
    return FALLBACK


# --------------------------------------------------------------------------
# 字段定义
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """一个可按协议端分别设置的字段。"""

    key: str                      # profiles.<组>.<key>
    shared: str                   # shared 模式下的旧路径
    kind: str                     # bool / int / string
    label: str                    # 面板上的中文名
    options: tuple[str, ...] = ()  # string 型的可选值
    hint: str = ""
    #: ``True`` 表示只显示在 OneBot 组，
    #: ``False`` 只显示在官机组，``None`` 三组都显示
    only_in: str | None = None


FIELDS: tuple[Field, ...] = (
    Field(
        key="send_mode",
        shared="plugin.send_mode",
        kind="string",
        label="视频发送方式",
        options=("url", "download"),
        hint="url=发直链（省流量、对面要点开）；download=先下载再作为文件发出。",
    ),
    Field(
        key="max_images",
        shared="plugin.max_images",
        kind="int",
        label="单条最多图片数",
        hint="超出会拆成多条（官方机器人还受平台「一次一张」限制）。",
    ),
    Field(
        key="max_videos",
        shared="plugin.max_videos",
        kind="int",
        label="单条最多视频数",
    ),
    Field(
        key="send_as_forward",
        shared="plugin.send_as_forward",
        kind="bool",
        label="用聊天记录（合并转发）发送",
        hint="把简介和媒体打包成一条合并转发。"
             "**QQ 官方机器人没有这个能力**，所以只在本组显示。",
        only_in=ONEBOT,
    ),
    Field(
        key="album_forward_when_exceed",
        shared="plugin.album_forward_when_exceed",
        kind="bool",
        label="图集超限时合并成聊天记录",
        hint="图片超过上面的上限时，用一条合并转发完整送出，而不是砍掉多余的。",
        only_in=ONEBOT,
    ),
    Field(
        key="show_desc",
        shared="plugin.show_desc",
        kind="bool",
        label="一起发送文字描述",
        hint="解析结果里的标题 / 简介要不要跟着媒体发出来。",
    ),
    Field(
        key="reply_on_error",
        shared="plugin.reply_on_error",
        kind="bool",
        label="解析失败时回复提示",
        hint="关掉则失败时静默（群比较吵的时候用）。",
    ),
    Field(
        key="only_group",
        shared="plugin.only_group",
        kind="bool",
        label="只在群聊生效",
    ),
    Field(
        key="download_concurrency",
        shared="plugin.download_concurrency",
        kind="int",
        label="媒体下载并发数",
        hint="官方机器人接口对并发更敏感，默认给得保守一些。",
    ),
    Field(
        key="music_send_mode",
        shared="music.sendMode",
        kind="string",
        label="点歌发送方式",
        options=("link", "card", "voice"),
        hint="link=链接；card=音乐卡片（**仅 OneBot**）；voice=语音条。"
             "官方机器人没有音乐卡片，默认用语音。",
    ),
    Field(
        key="music_search_mode",
        shared="music.searchMode",
        kind="string",
        label="点歌方式",
        options=("list", "direct"),
        hint="list=先出列表、回序号点播；direct=直接送第一首。",
    ),
    Field(
        key="md_image_width",
        shared="plugin.mdImageWidth",
        kind="int",
        label="MD 内嵌图宽度（px）",
        hint="官方机器人专属。markdown 里的图片**必须带尺寸**，"
             "不带时电脑端能看到、手机端只显示 [alt]。图片按真实比例缩放，"
             "点开 / 保存仍是原图。建议 200~400。",
        only_in=QQOFFICIAL,
    ),
    Field(
        key="qq_buttons",
        shared="plugin.qqButtons",
        kind="bool",
        label="菜单 / 列表下挂按钮",
        hint="官方机器人专属。自定义按钮需要平台开通该能力，"
             "发不出去时插件会自动跳过，不影响内容本身。",
        only_in=QQOFFICIAL,
    ),
    Field(
        key="enable_sign_proxy",
        shared="music.enableSignProxy",
        kind="bool",
        label="启用音乐卡片签名代理",
        hint="OneBot 专属：协议端发音乐卡片要外部签名服务，而其响应是双重编码的，"
             "插件内置代理帮忙展开。官方机器人用不到。",
        only_in=ONEBOT,
    ),
)

_FIELD_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}
_FIELD_BY_SHARED: dict[str, Field] = {f.shared: f for f in FIELDS}

#: 每个分组要在 schema 里呈现的字段（``only_in`` 决定归属）
GROUP_FIELDS: dict[str, tuple[Field, ...]] = {
    ONEBOT: tuple(f for f in FIELDS if f.only_in in (None, ONEBOT)),
    QQOFFICIAL: tuple(f for f in FIELDS if f.only_in in (None, QQOFFICIAL)),
    FALLBACK: tuple(f for f in FIELDS if f.only_in is None),
}

# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------
#
# 原则：**等于当前实际生效的行为**。改动这里等于改动所有新装用户的行为，
# 所以每一条都要能说清「为什么这个平台是这个值」。

#: 三组共用的基线
_BASE: dict[str, Any] = {
    "send_mode": "url",
    "max_images": 9,
    "max_videos": 9,
    "send_as_forward": False,
    "album_forward_when_exceed": True,
    "show_desc": True,
    "reply_on_error": False,
    "only_group": False,
    "download_concurrency": 8,
    "music_send_mode": "link",
    "music_search_mode": "list",
}

#: 每个分组的差异
_OVERRIDES: dict[str, dict[str, Any]] = {
    ONEBOT: {
        # 能发合并转发、能发音乐卡片 —— 保持旧默认
        "enable_sign_proxy": True,
    },
    QQOFFICIAL: {
        # 官方没有合并转发消息段，配置成 true 也只会让整条发送失败
        "send_as_forward": False,
        "album_forward_when_exceed": False,
        # 官方没有 music 消息段 → 语音是唯一能「听到歌」的形态
        "music_send_mode": "voice",
        # 官方接口对并发更敏感，且 media 一次只挂一个
        "download_concurrency": 4,
        "md_image_width": 300,
        "qq_buttons": True,
    },
    FALLBACK: {
        "send_as_forward": False,
        "album_forward_when_exceed": False,
        "music_send_mode": "link",
    },
}


def defaults(group: str) -> dict[str, Any]:
    """某个分组的完整默认值（基线 + 该组差异）。"""
    out = dict(_BASE)
    out.update(_OVERRIDES.get(group, {}))
    return out


#: 各旧键在 ``_conf_schema.json`` 里的默认值。
#:
#: 用来判断「用户到底改过旧键没有」—— 用户配置文件里的值和 schema 默认一致
#: 就说明他没动过这一项。**必须与 ``_conf_schema.json`` 保持一致**，
#: ``tests/test_platform_profiles.py`` 有一条断言比对两者。
SHARED_DEFAULTS: dict[str, Any] = {
    "plugin.send_mode": "url",
    "plugin.max_images": 9,
    "plugin.max_videos": 9,
    "plugin.send_as_forward": False,
    "plugin.album_forward_when_exceed": True,
    "plugin.show_desc": True,
    "plugin.reply_on_error": False,
    "plugin.only_group": False,
    "plugin.download_concurrency": 8,
    "plugin.mdImageWidth": 300,
    "plugin.qqButtons": True,
    "music.sendMode": "link",
    "music.searchMode": "list",
    "music.enableSignProxy": True,
}


# --------------------------------------------------------------------------
# 读配置
# --------------------------------------------------------------------------


def mode(profiles: Any) -> str:
    """配置里的「配置来源」。认不出的值一律按 ``per_platform`` 处理。"""
    if not isinstance(profiles, dict):
        return MODE_PER_PLATFORM
    raw = str(profiles.get("mode") or "").strip().lower()
    return MODE_SHARED if raw == MODE_SHARED else MODE_PER_PLATFORM


def is_shared(profiles: Any) -> bool:
    return mode(profiles) == MODE_SHARED


def _coerce(raw: Any, field: Field, fallback: Any) -> Any:
    """把面板 / 手改配置文件里的值转成正确类型。

    WebUI 的 int 输入框可能给出字符串；手改 JSON 时 bool 可能写成 ``"true"``。
    转不动就回退到 ``fallback``，**绝不抛异常**（配置读取在热路径上）。

    ⚠️ string 型**不校验 options**：调用方本来就有兜底
    （比如 ``_music_send_mode`` 会 ``if mode in ...LINK else "link"``），
    在这里把不认识的值吃掉，反而会让用户写的配置**静默消失**。
    """
    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "on", "yes", "开", "启用"):
            return True
        if text in ("false", "0", "off", "no", "关", "禁用", ""):
            return False
        return bool(fallback)

    if field.kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    return str(raw).strip() or fallback


def value(
    profiles: Any,
    caps_key: str | None,
    field_key: str,
    shared_get: Callable[[str, Any], Any] | None = None,
    default: Any = None,
) -> Any:
    """取某个字段在**当前协议端**下的生效值。

    取值规则（顺序很重要，直接决定升级会不会改变用户现有行为）：

    1. ``shared`` 模式 → 只读旧路径 ``field.shared``，读不到就用组默认；
    2. ``per_platform`` 模式（默认）：

       a. 面板里 ``profiles.<组>.<字段>`` **如果不是该组默认值** → 用户改过
          → 用它（面板优先）；
       b. 面板还是默认值（说明用户没碰过这一项）→ **看旧键改过没有**：
          改过就用旧键的值 —— 这保证了「升级到分协议端配置」不会让老用户
          已经调好的 ``plugin.send_as_forward`` 之类突然失效；
       c. 两边都是默认 → 用该组的默认值。

    第 2b 步是关键：没有它，升级后老用户改过的配置会被静默忽略，
    表现为「我明明开了合并转发，怎么不发聊天记录了」。

    Args:
        profiles: 插件配置里的 ``profiles`` 分组。
        caps_key: 当前事件的协议端 key（``platform_key(event)``）。
        field_key: 字段名（见 ``FIELDS``）。
        shared_get: 读旧配置的回调（一般传 ``Main.conf_get``）。
            传 ``None`` 就只能拿到组默认值 —— 单测时方便。
        default: 字段不认识时的返回值。
    """
    field = _FIELD_BY_KEY.get(field_key)
    if field is None:
        return default

    group = group_for(caps_key)
    group_default = defaults(group).get(field_key, default)

    # ---- 1) 通用模式：回到旧路径 ----
    if is_shared(profiles):
        if shared_get is not None:
            got = shared_get(field.shared, None)
            if got is not None:
                return _coerce(got, field, group_default)
        return group_default

    # ---- 2a) 面板里显式改过 → 面板优先 ----
    #
    # ⚠️ 判断用 `is not None` 而不是真值：`False` / `0` / `""`
    # 都是**有效配置**（官机的 send_as_forward=False 就是显式关掉了）。
    node = profiles.get(group) if isinstance(profiles, dict) else None
    panel_raw = node.get(field_key) if isinstance(node, dict) else None
    if panel_raw is not None:
        panel_val = _coerce(panel_raw, field, group_default)
        if panel_val != group_default:
            return panel_val

    # ---- 2b) 面板还是默认值 → 继承用户改过的旧键（升级兼容）----
    if shared_get is not None:
        old_raw = shared_get(field.shared, None)
        if old_raw is not None:
            old_default = SHARED_DEFAULTS.get(field.shared, group_default)
            if _coerce(old_raw, field, old_default) != old_default:
                return _coerce(old_raw, field, group_default)

    # ---- 2c) 都没动过 → 组默认 ----
    return group_default


def save_path(profiles: Any, caps_key: str | None, shared_path: str) -> str:
    """把一个**旧配置路径**映射到「当前该写到哪里」。

    ``#R配置`` 这类聊天命令是按旧路径写的（``plugin.send_as_forward``）。
    在 ``per_platform`` 模式下要把写入重定向到 ``profiles.<组>.<key>``，
    否则用户改了配置却不生效 —— 这是最容易踩的坑。

    不是本表里的路径（Cookie、平台开关之类）原样返回。
    """
    field = _FIELD_BY_SHARED.get(str(shared_path))
    if field is None or is_shared(profiles):
        return str(shared_path)
    return f"profiles.{group_for(caps_key)}.{field.key}"


def describe(
    profiles: Any,
    caps_key: str | None,
    shared_get: Callable[[str, Any], Any] | None = None,
) -> str:
    """给 ``#R配置`` 用的可读描述：当前这份配置档都设了什么。"""
    group = group_for(caps_key)
    lines = [
        f"🧩 配置来源：{'通用（plugin/music 里的旧值）' if is_shared(profiles) else '分协议端'}",
        f"　　当前协议端：{GROUP_LABELS.get(group, group)}",
    ]
    if is_shared(profiles):
        return "\n".join(lines)
    for field in GROUP_FIELDS[group]:
        got = value(profiles, caps_key, field.key, shared_get)
        lines.append(f"　　{field.label}：{_show(got)}")
    return "\n".join(lines)


def _show(got: Any) -> str:
    if isinstance(got, bool):
        return "开" if got else "关"
    return str(got)


def schema_keys() -> set[str]:
    """``profiles`` 分组里**所有**该出现在 schema 的键（含 ``mode``）。

    给测试用：拿它和 ``_conf_schema.json`` 比对，防止「只加代码不加 schema」
    ——那样配置会被 AstrBot 静默裁掉。
    """
    keys = {"mode"}
    for group, fields in GROUP_FIELDS.items():
        keys.add(group)
        for field in fields:
            keys.add(f"{group}.{field.key}")
    return keys
