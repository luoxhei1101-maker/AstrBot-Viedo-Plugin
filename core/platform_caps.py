"""平台能力表：不同协议端能发什么、不能发什么。

**为什么要有这一层**

不同「协议端」的消息能力差别很大，而插件里很多发送形态（合并转发、
音乐卡片）是**某个协议端专属**的：

=========================  ==========  ==============  ==========  ========
能力                       OneBot v11  QQ 官方机器人   说明        实现
=========================  ==========  ==============  ==========  ========
合并转发 ``Comp.Nodes``    ✅          ❌              官方无此段  ``forward``
音乐卡片 ``Comp.Music``    ✅          ❌              官方无此段  ``music_card``
原生 markdown              ❌          ✅              ``msg_type=2``  ``markdown``
消息按钮 ``keyboard``      ❌          ✅（需自定义）  挂在 MD 上  ``keyboard``
语音 ``Comp.Record``       ✅          ✅              c2c/群语音  ``voice``
=========================  ==========  ==============  ==========  ========

（官方机器人的能力是实测 AstrBot 源码来的：``qqofficial_message_event.py``
只处理 ``File / Image / Plain / Record / Video``，**没有 Node/Nodes 分支**；
``use_markdown(True)`` 会让它走 ``msg_type=2`` + ``MarkdownPayload``。
按钮同理 —— 适配器不认 keyboard，所以插件绕过它自己打接口，
见 ``core/qq_buttons.py`` 和 ``main._send_qq_payload``。）

**设计原则**：所有「发送形态」的决策都来这里查表，而不是在 main.py 里
散落 ``if 官方机器人`` 的判断。加新协议端时只改这一个文件。

**配置覆盖**：用户可以在插件配置里为某个平台实例（``platform_id``）单独
覆盖能力（见 ``platformProfiles``），例如把某个 OneBot 的合并转发也关掉。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any


@dataclass(frozen=True)
class PlatformCaps:
    """一个协议端实例的发送能力。"""

    key: str
    label: str
    # ---- 发送形态 ----
    forward: bool       # 能用合并转发（Comp.Nodes）
    music_card: bool    # 能用音乐卡片（Comp.Music）
    markdown: bool      # 能发原生 markdown
    keyboard: bool      # 能挂「消息按钮」（keyboard）—— 必须和 markdown 一起发
    # ---- 媒体类型 ----
    voice: bool
    video: bool
    image: bool
    # ---- 细节限制 ----
    # 一条消息里最多几张图。官方机器人的 media 一次只挂一张，
    # 所以多图必须拆成多条发（见 main.py 的 _send_images）。
    max_images_per_msg: int = 9


# --------------------------------------------------------------------------
# 内置能力表
# --------------------------------------------------------------------------

# 未知协议端走**保守**取值：不发合并转发、不发音乐卡片。
#
# 为什么保守：`Comp.Nodes`（合并转发）和 `Comp.Music`（音乐卡片）都是
# **QQ 生态特有**的消息段，非 QQ 平台（Telegram / Discord / 钉钉…）接了
# 只会让整条消息链发送失败。而「降级成直发」最坏也只是消息分成几条，
# 不会丢内容 —— 两害相权取其轻。
_DEFAULT = PlatformCaps(
    key="unknown", label="未知协议端",
    forward=False, music_card=False, markdown=False, keyboard=False,
    voice=True, video=True, image=True,
)

_BUILTIN: dict[str, PlatformCaps] = {
    # OneBot v11（NapCat / Lagrange / go-cqhttp 等）
    "aiocqhttp": PlatformCaps(
        key="aiocqhttp", label="OneBot v11",
        forward=True, music_card=True, markdown=False, keyboard=False,
        voice=True, video=True, image=True,
    ),
    # QQ 官方机器人（botpy）
    "qqofficial": PlatformCaps(
        key="qqofficial", label="QQ 官方机器人",
        forward=False, music_card=False, markdown=True, keyboard=True,
        voice=True, video=True, image=True,
        # 官方机器人的 media 一次一张，多图要拆开发
        max_images_per_msg=1,
    ),
    # 官方机器人 webhook 版：能力与上面一致
    "qqofficial_webhook": PlatformCaps(
        key="qqofficial_webhook", label="QQ 官方机器人(Webhook)",
        forward=False, music_card=False, markdown=True, keyboard=True,
        voice=True, video=True, image=True,
        max_images_per_msg=1,
    ),
    # 其它协议端（微信、钉钉等）保守取值：不发卡片、不发合并转发
    "wechatpadpro": PlatformCaps(
        key="wechatpadpro", label="微信",
        forward=False, music_card=False, markdown=False, keyboard=False,
        voice=True, video=True, image=True,
    ),
}

# 名字里带这些关键字就认为是官方机器人（``get_platform_name()`` 的写法
# 在不同版本里可能是 qqofficial / qq_official / qq-official）
_QQOFFICIAL_HINTS = ("qqofficial", "qq_official", "qq-official")

# 可被配置覆盖的字段名（也是 platformProfiles 里认的键）。
#
# ⚠️ 注意 `f.type` 在 `from __future__ import annotations` 下是**字符串**
# （``"bool"``）而不是类型对象 —— 只写 `f.type is bool` 会得到空元组，
# 表现为「配置覆盖静默失效」。所以两种形式都要认。
OVERRIDABLE = tuple(
    f.name for f in fields(PlatformCaps)
    if f.name not in ("key", "label") and f.type in (bool, "bool")
)


def platform_key(event: Any) -> str:
    """取事件所属的平台名（小写）。

    ``get_platform_name()`` 在官方适配器里返回 ``"qqofficial"``，
    OneBot 那边是 ``"aiocqhttp"``。
    """
    try:
        name = event.get_platform_name() or ""
    except Exception:  # noqa: BLE001 - 探测性质，拿不到就当未知
        name = ""
    name = str(name).strip().lower()
    for hint in _QQOFFICIAL_HINTS:
        if hint in name:
            return "qqofficial"
    return name or "unknown"


def platform_id(event: Any) -> str:
    """取平台实例 ID（用户在 AstrBot 里配置的实例名，如 ``qq_official_1``）。"""
    try:
        return str(event.get_platform_id() or "")
    except Exception:  # noqa: BLE001
        return ""


def _profile_for(profiles: Any, pid: str) -> dict:
    """从 platformProfiles 配置里挑出该实例的覆盖项。

    配置形态是 ``list[dict]``，每项形如::

        {"platform_id": "qq_official_1", "forward": false, "music_card": false}

    同时兼容 ``dict``（``{"qq_official_1": {...}}``），手改配置文件时更省事。
    """
    if not profiles or not pid:
        return {}

    if isinstance(profiles, dict):
        got = profiles.get(pid)
        return dict(got) if isinstance(got, dict) else {}

    if isinstance(profiles, list):
        for item in profiles:
            if not isinstance(item, dict):
                continue
            if str(item.get("platform_id") or "").strip() == pid:
                return dict(item)
    return {}


def caps_for(event: Any, profiles: Any = None) -> PlatformCaps:
    """按事件取能力表，并套上该平台实例的配置覆盖。

    Args:
        event: AstrBot 消息事件。
        profiles: 插件配置里的 ``platformProfiles``（可选）。
    """
    key = platform_key(event)
    caps = _BUILTIN.get(key, _DEFAULT)

    override = _profile_for(profiles, platform_id(event))
    if override:
        patch = {}
        for name in OVERRIDABLE:
            if name in override and isinstance(override[name], bool):
                patch[name] = override[name]
        if patch:
            caps = replace(caps, **patch)
    return caps


def describe(caps: PlatformCaps) -> str:
    """给 /R配置 用的可读描述。"""
    def mark(v: bool) -> str:
        return "✅" if v else "❌"

    return (
        f"{caps.label}（{caps.key}）\n"
        f"  合并转发 {mark(caps.forward)}   音乐卡片 {mark(caps.music_card)}"
        f"   原生MD {mark(caps.markdown)}   消息按钮 {mark(caps.keyboard)}\n"
        f"  语音 {mark(caps.voice)}   视频 {mark(caps.video)}"
        f"   图片 {mark(caps.image)}（单条上限 {caps.max_images_per_msg} 张）"
    )
