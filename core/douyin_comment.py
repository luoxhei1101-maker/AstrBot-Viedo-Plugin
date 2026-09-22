"""抖音评论抓取 + 转文本/图片合并转发节点。

抖音评论接口 ``aweme/v1/web/comment/list`` 需要 ``a_bogus`` 签名参数
（见 ``core/a_bogus.py``），否则返回验证失败。签名绑定 User-Agent，
所以请求 UA 必须和生成签名时用的完全一致。

评论里的图片（作者常用纯图评论）在 ``image_list``，结构见
``_comment_image_candidates`` 的说明。
"""

from __future__ import annotations

import datetime

from astrbot.api import logger

from .a_bogus import generate_a_bogus
from .constants import DY_COMMENT
from .douyin_ssr import rank_image_candidates
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


def _comment_image_candidates(item: dict) -> list[list[str]]:
    """取出一条评论里每张图的**候选 URL 列表**（原图优先）。

    实测（2026-09-22，作品 7688010612275061413 的作者纯图评论）：

    ==================  ==========  ===========  ============
    字段                体积        尺寸         结论
    ==================  ==========  ===========  ============
    ``origin_url``      660 KB     1600×1600    **原图，用这个**
    ``medium_url``      88 KB      332×332      缩略
    ``thumb_url``       21 KB      124×124      缩略
    ``crop_url``        29 KB      124×166      裁切缩略
    ``download_url``    ——          403        **别碰**
    ==================  ==========  ===========  ============

    ``origin_url`` 本身有 4 个 CDN 候选，后缀分 ``.image``（PNG）和
    ``.jpeg`` 两种 —— 实测都是 1600×1600 原图，但 ``.jpeg`` 只有 660 KB
    （PNG 是 804 KB）。所以复用图集那套 ``rank_image_candidates``
    （``.jpeg`` 排在 ``.image`` 前面）顺手就把体积也优化了。
    """
    out: list[list[str]] = []
    for img in item.get("image_list") or []:
        if not isinstance(img, dict):
            continue
        urls = (img.get("origin_url") or {}).get("url_list") or []
        cands: list[str] = []
        for u in urls:
            if u and u not in cands:
                cands.append(u)
        if cands:
            out.append(rank_image_candidates(cands))
    return out


def _normalize_comment(item: dict) -> dict | None:
    """把一条抖音评论转成 ``{nickname, text, images}``。

    **只要文字或图片有一个就保留** —— 以前这里是「没文字就 return None」，
    结果作者那种「只发图不配字」的评论被整条丢掉（用户就是想看这种）。
    """
    user = item.get("user") or {}
    text = str(item.get("text") or "").strip()
    images = _comment_image_candidates(item)

    if not text and not images:
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

    if text and meta:
        body = f"{text}\n—— {meta}"
    elif text:
        body = text
    else:
        # 纯图评论：正文为空，只留一行 meta（不留空行）
        body = f"—— {meta}" if meta else ""

    return {
        "nickname": nickname,
        "text": body,
        "images": images,
    }


async def fetch_douyin_comments(
    dou_id,
    *,
    cookie: str = "",
    limit: int = 5,
) -> list[dict]:
    """抓抖音某作品（aweme_id=dou_id）的评论，返回 ``[{nickname, text, images}]``。

    ``images`` 是「每张图的候选 URL 列表」的列表，发送端逐个候选回退下载。

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
