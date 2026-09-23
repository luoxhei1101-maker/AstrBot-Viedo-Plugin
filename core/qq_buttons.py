"""QQ 官方机器人「按钮」（keyboard）的纯数据构建。

⚠️ **为什么需要这一层**：AstrBot 的 ``qqofficial`` 适配器**完全不支持
keyboard** —— ``_parse_to_qqofficial`` 只认 ``Plain / Image / Record /
Video / File``，其余一律走 ``logger.debug("qq_official 忽略 ...")``，
消息里带什么按钮组件都会被丢掉。所以按钮只能在插件里自己拼 payload、
**绕过适配器直接调 QQ 的 HTTP 接口**（见 ``main._send_qq_payload``）。

本模块只负责「拼数据结构」，不碰 event / 网络 —— 方便单测。

官方字段限制（``bot.q.qq.com`` 的「消息按钮」+「发送群聊消息」两页）：

======================  ==================================================
最多 5 行               每行最多 5 个按钮
``render_data.label``   **最多 10 字符**（超了会被客户端截断）
``action.type``         0=跳转链接 / 1=回调后台 / 2=指令
``action.enter``        **仅单聊可用**：true=点击后直接发送 data；
                        群里点击只会把 ``@机器人 data`` 插进输入框
``action.reply``        指令是否带「引用本消息」发出
``permission.type``     0=指定用户 / 1=仅管理员 / 2=所有人
``unsupport_tips``      客户端版本过低时显示的文案
======================  ==================================================

**按钮必须挂在 markdown 消息上**（官方原话：「在 markdown 消息的基础上，
支持消息最底部挂载按钮」），所以本模块的产物要和 ``markdown`` 一起发。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

# ---- 官方硬限制 ----
MAX_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_LABEL_CHARS = 10

# render_data.style：0 灰色线框 / 1 蓝色线框 / 3 白底红字 / 4 蓝底白字
STYLE_GRAY = 0
STYLE_BLUE = 1

# permission.type
PERM_SPECIFY_USERS = 0
PERM_ADMIN_ONLY = 1
PERM_EVERYONE = 2

# action.type
ACTION_JUMP = 0
ACTION_CALLBACK = 1
ACTION_COMMAND = 2

DEFAULT_UNSUPPORT_TIPS = "你的 QQ 版本过低，请升级后再点按钮"


def button(
    label: str,
    data: str = "",
    *,
    enter: bool = False,
    style: int = STYLE_BLUE,
    reply: bool = False,
    permission: int = PERM_EVERYONE,
    unsupport_tips: str = DEFAULT_UNSUPPORT_TIPS,
) -> dict[str, Any]:
    """拼一个「指令按钮」（action.type=2）。

    :param label: 按钮文字。**超过 10 字符会被截断**（官方硬限制，不是我们偷懒）。
    :param data: 点击后插进输入框 / 直接发送的内容，比如 ``"#R菜单"``。
    :param enter: 点击后**直接发送**（仅单聊有效）。留 False 就是「插进输入框
        等你补参数」—— 点歌按钮要的就是这个行为。
    """
    text = (label or "").strip()[:MAX_LABEL_CHARS]
    return {
        "render_data": {
            "label": text,
            "visited_label": text,
            "style": style,
        },
        "action": {
            "type": ACTION_COMMAND,
            "permission": {"type": permission},
            "data": data,
            "reply": bool(reply),
            "enter": bool(enter),
            "unsupport_tips": unsupport_tips,
        },
    }


def link_button(
    label: str,
    url: str,
    *,
    style: int = STYLE_GRAY,
    permission: int = PERM_EVERYONE,
    unsupport_tips: str = DEFAULT_UNSUPPORT_TIPS,
) -> dict[str, Any]:
    """拼一个「跳转按钮」（action.type=0）—— 打开 http(s) 或小程序。"""
    text = (label or "").strip()[:MAX_LABEL_CHARS]
    return {
        "render_data": {
            "label": text,
            "visited_label": text,
            "style": style,
        },
        "action": {
            "type": ACTION_JUMP,
            "permission": {"type": permission},
            "data": url,
            "unsupport_tips": unsupport_tips,
        },
    }


def keyboard(*rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """把若干「行」拼成消息体里的 ``keyboard`` 字段。

    行数 / 每行按钮数都按官方上限**静默截断**，不抛异常 —— 按钮是锦上添花，
    不值得因为它把整条消息搞挂。没有有效按钮时返回 ``None``，调用方直接
    按「不带按钮」发。
    """
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        btns = [b for b in row if isinstance(b, dict)][:MAX_BUTTONS_PER_ROW]
        if btns:
            clean.append({"buttons": btns})
        if len(clean) >= MAX_ROWS:
            break
    if not clean:
        return None
    return {"content": {"rows": clean}}


def inline_cmd(label: str, command: str, *, enter: bool = False) -> str:
    """QQ markdown 的「内联指令」：**点击文字就能填进输入框 / 直接发送**。

    官方文档「文本交互 → 指令操作」（**仅在 markdown 支持**）给了两种：

    * ``enter=True`` —— **回车指令**：点击后把 ``command`` 直接发出去。
      ⚠️ 官方注明**群聊不支持**这个能力（只有单聊 / 频道可用）。
    * ``enter=False`` —— **参数指令**：点击后把 ``command`` **插入输入框**，
      由用户自己编辑发送 —— 群聊里要的就是这个。

    用法：点歌列表每行写成 ``[1. 歌名](mqqapi://...)``，用户**点歌名**就把
    序号填进输入框。列表看起来还是普通文字列表，**不需要底部按钮**。

    ⭐ ``command`` 必须 url 编码 —— 带空格 / 中文时不编码会被截断。
    """
    url = (
        f"mqqapi://aio/inlinecmd?command={quote(command, safe='')}"
        f"&enter={'true' if enter else 'false'}&reply=false"
    )
    return f"[{label}]({url})"
