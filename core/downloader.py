"""媒体下载工具。

原版用的是 ``utils/common.js`` 里的 downloadImg / downloadVideo 那一套，
把文件落到 Yunzai 的临时目录再发出去。

AstrBot 这边有个更省事的机制：``event.track_temporary_local_files()``，
把文件登记上去，事件结束后框架会自动清理，不用自己写定时清理任务。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger

from .http import HttpError, download_bytes

# 常见媒体后缀，用来决定落到本地时用什么扩展名
_MEDIA_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def _guess_ext(url: str, content_type: str = "") -> str:
    """先看响应头，再看 URL 路径，最后兜底 .bin。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _MEDIA_EXT:
        return _MEDIA_EXT[ct]

    path = urlparse(url).path
    suffix = os.path.splitext(path)[1].lower()
    if suffix and len(suffix) <= 5:
        return suffix
    return ".bin"


def _temp_dir() -> Path:
    """插件专属临时目录，避免和别的插件抢同一个目录。"""
    d = Path(tempfile.gettempdir()) / "astrbot_plugin_rconsole_lite"
    d.mkdir(parents=True, exist_ok=True)
    return d


class MediaTooLarge(Exception):
    """超出配置的大小上限。"""


async def download_media(
    url: str,
    *,
    prefix: str = "media",
    max_bytes: int = 0,
    timeout: float = 120.0,
) -> Path:
    """把远程媒体下载到本地临时目录。

    Args:
        max_bytes: 大于 0 时做大小限制，超限抛 ``MediaTooLarge``。
    """
    try:
        body = await download_bytes(url, timeout=timeout)
    except HttpError as exc:
        raise HttpError(f"媒体下载失败: {url}") from exc

    if max_bytes > 0 and len(body) > max_bytes:
        raise MediaTooLarge(
            f"媒体大小 {len(body) / 1024 / 1024:.1f}MB 超过上限 {max_bytes / 1024 / 1024:.1f}MB"
        )

    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    filename = f"{prefix}_{digest}{_guess_ext(url)}"
    path = _temp_dir() / filename
    path.write_bytes(body)

    logger.debug(f"[R插件移植版] 已下载 {len(body) / 1024:.0f}KB -> {path}")
    return path
