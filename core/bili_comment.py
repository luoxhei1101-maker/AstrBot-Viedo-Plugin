"""B 站评论抓取 + 转文本合并转发节点。

原版评论是「抓评论 → 渲染 HTML → puppeteer 截图」，截图那条路依赖
puppeteer 且要重做整套 HTML 模板，移植成本高、收益低。这里用原版
**截图失败时的兜底方案**作为主实现：抓评论后转成文本，用合并转发
（聊天记录）发出来——每条评论一条转发记录，昵称 / 内容 / 时间 / 点赞都在。

抖音评论没做：它依赖 ``a-bogus`` 签名（见 ``platforms/douyin.py`` 的说明）。
微博评论原版本来就是文本合并转发，后续需要再补。
"""

from __future__ import annotations

import datetime

from astrbot.api import logger

from .bili_wbi import signed_get_json
from .http import HttpError, fetch_json

# B 站评论接口：WBI 主接口 + 经典接口兜底（原版也是这个双路）
_WBI_MAIN = "https://api.bilibili.com/x/v2/reply/wbi/main"
_REPLY_PAGE = "https://api.bilibili.com/x/v2/reply"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def _fmt_count(n) -> str:
    """数字转成「1.2万」「3.4亿」这种可读形式。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿".replace(".0", "")
    if n >= 10000:
        return f"{n / 10000:.1f}万".replace(".0", "")
    return str(n)


def _fmt_time(ctime) -> str:
    """秒级时间戳转成「MM-DD HH:MM」。"""
    try:
        dt = datetime.datetime.fromtimestamp(int(ctime))
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return ""


def _normalize_comment(item: dict) -> dict | None:
    """把一条 B 站评论转成 ``{nickname, text}``，无正文则返回 None。"""
    member = item.get("member") or {}
    content = item.get("content") or {}
    message = str(content.get("message") or "").strip()
    if not message:
        return None

    nickname = member.get("uname") or "B站用户"
    level = (member.get("level_info") or {}).get("current_level")
    like = _fmt_count(item.get("like") or 0)
    time = _fmt_time(item.get("ctime") or 0)
    location = (item.get("reply_control") or {}).get("location") or ""
    location = location.replace("IP属地：", "").replace("IP属地:", "").strip()

    # 底部一行 meta：Lv 等级 · 时间 · 地点 · 赞数
    meta_parts = []
    if level:
        meta_parts.append(f"Lv{level}")
    if time:
        meta_parts.append(time)
    if location:
        meta_parts.append(location)
    if like:
        meta_parts.append(f"赞 {like}")
    meta = " · ".join(meta_parts)

    text = message if not meta else f"{message}\n—— {meta}"
    return {"nickname": nickname, "text": text}


async def fetch_bili_comments(
    oid,
    *,
    limit: int = 5,
    cookie: str = "",
) -> list[dict]:
    """抓 B 站某视频（aid=oid）的热门评论，返回 ``[{nickname, text}]``。

    优先 WBI 主接口，失败或返回非 0 时退回经典接口（原版同样双路）。
    全程不抛异常（评论是附加功能，失败不该拖垮主流程），顶多返回空列表。
    """
    if not oid:
        return []

    headers = dict(_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    params = {"type": 1, "oid": oid, "mode": 3, "next": 0, "ps": limit}

    data = None
    try:
        data = await signed_get_json(_WBI_MAIN, params, headers=headers, retries=1)
    except HttpError as exc:
        logger.debug(f"[R插件][B站评论] WBI 接口失败，回退经典接口: {exc}")

    if not data or data.get("code") != 0:
        url = (
            f"{_REPLY_PAGE}?type=1&oid={oid}&sort=1&ps={limit}&pn=1&nohot=0"
        )
        try:
            data = await fetch_json(url, headers=headers, retries=1)
        except HttpError as exc:
            logger.debug(f"[R插件][B站评论] 经典接口也失败，跳过评论: {exc}")
            return []

    if not data or data.get("code") != 0:
        return []

    payload = data.get("data") or {}
    replies = (payload.get("replies") or []) + (payload.get("hots") or [])

    seen: set = set()
    result: list[dict] = []
    for item in replies:
        rpid = item.get("rpid")
        if rpid in seen:
            continue
        seen.add(rpid)
        normalized = _normalize_comment(item)
        if normalized:
            result.append(normalized)
    return result
