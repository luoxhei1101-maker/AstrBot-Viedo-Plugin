"""抖音评论抓取 + 转文本合并转发节点。

抖音评论接口 ``aweme/v1/web/comment/list`` 需要 ``a_bogus`` 签名参数
（见 ``core/a_bogus.py``），否则返回验证失败。签名绑定 User-Agent，
所以请求 UA 必须和生成签名时用的完全一致。
"""

from __future__ import annotations

import datetime

from astrbot.api import logger

from .a_bogus import generate_a_bogus
from .constants import DY_COMMENT
from .http import HttpError, fetch_json

# 要和 DY_COMMENT 里的 browser_version（124.0.0.0）保持一致
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}


def _fmt_count(n) -> str:
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
    try:
        dt = datetime.datetime.fromtimestamp(int(ctime))
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return ""


def _normalize_comment(item: dict) -> dict | None:
    user = item.get("user") or {}
    text = str(item.get("text") or "").strip()
    if not text:
        return None

    nickname = user.get("nickname") or "抖音用户"
    like = _fmt_count(item.get("digg_count") or 0)
    time = _fmt_time(item.get("create_time") or 0)
    location = str(item.get("ip_label") or "").strip()

    meta_parts = []
    if time:
        meta_parts.append(time)
    if location:
        meta_parts.append(location)
    if like:
        meta_parts.append(f"赞 {like}")
    meta = " · ".join(meta_parts)

    return {
        "nickname": nickname,
        "text": text if not meta else f"{text}\n—— {meta}",
    }


async def fetch_douyin_comments(
    dou_id,
    *,
    cookie: str = "",
    limit: int = 5,
) -> list[dict]:
    """抓抖音某作品（aweme_id=dou_id）的评论，返回 ``[{nickname, text}]``。

    全程不抛异常（评论是附加功能），失败顶多返回空列表。
    """
    if not dou_id:
        return []

    url = DY_COMMENT.replace("{}", str(dou_id))
    query = url.split("?", 1)[1]
    ua = _HEADERS["User-Agent"]

    headers = dict(_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    try:
        ab = await generate_a_bogus(query, ua)
    except RuntimeError as exc:
        logger.debug(f"[R插件][抖音评论] {exc}")
        return []

    full = f"{url}&a_bogus={ab}"
    try:
        data = await fetch_json(full, headers=headers, retries=1)
    except HttpError as exc:
        logger.debug(f"[R插件][抖音评论] 接口失败，跳过: {exc}")
        return []

    comments = data.get("comments") or (data.get("data") or {}).get("comments") or []

    result: list[dict] = []
    for item in comments[:limit]:
        normalized = _normalize_comment(item)
        if normalized:
            result.append(normalized)
    return result
