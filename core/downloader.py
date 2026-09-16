"""媒体下载工具。

原版用的是 ``utils/common.js`` 里的 downloadImg / downloadVideo 那一套，
把文件落到 Yunzai 的临时目录再发出去。

AstrBot 这边有个更省事的机制：``event.track_temporary_local_files()``，
把文件登记上去，事件结束后框架会自动清理，不用自己写定时清理任务。

**下载提速**：早期实现每次请求都新建 ``ClientSession`` + ``TCPConnector``，
连接不复用，图集几十张图串行下载非常慢。现在：

- ``download_many`` 用一个共享 session（连接池）并发下载，``Semaphore`` 限流
- 流式写文件（``iter_chunked`` 分块落盘），不再一次性 ``read()`` 进内存
- 支持 ``Content-Length`` 预判超限，超限直接放弃，不下完整文件
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger

from .http import BROWSER_HEADERS, HttpError, download_bytes

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

# 流式下载的分块大小
_CHUNK_SIZE = 64 * 1024


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


# 常见媒体站的防盗链 Referer 映射。很多平台的图片 URL（如抖音
# p3-sign.douyinpic.com）会校验 Referer，不带正确的来源就直接 403。
_REFERER_BY_HOST = {
    "douyinpic.com": "https://www.douyin.com/",
    "douyin.com": "https://www.douyin.com/",
    "iesdouyin.com": "https://www.douyin.com/",
    "amemv.com": "https://www.douyin.com/",
    "hdslb.com": "https://www.bilibili.com/",
    "bilibili.com": "https://www.bilibili.com/",
    "sinaimg.cn": "https://weibo.com/",
    "weibo.com": "https://weibo.com/",
    "kuaishou.com": "https://www.kuaishou.com/",
    "yximgs.com": "https://www.kuaishou.com/",
}


def _headers_for(url: str) -> dict[str, str]:
    """按 URL 域名补上防盗链需要的 Referer，其余沿用浏览器头。"""
    headers = dict(BROWSER_HEADERS)
    host = (urlparse(url).netloc or "").lower()
    for key, referer in _REFERER_BY_HOST.items():
        if key in host:
            headers["Referer"] = referer
            break
    return headers


def _temp_dir() -> Path:
    """插件专属临时目录，避免和别的插件抢同一个目录。"""
    d = Path(tempfile.gettempdir()) / "astrbot_plugin_rconsole"
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
    """把单个远程媒体下载到本地临时目录。

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

    logger.debug(f"[R插件] 已下载 {len(body) / 1024:.0f}KB -> {path}")
    return path


async def _stream_one(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefix: str,
    max_bytes: int,
) -> Path:
    """用共享 session 流式下载单个文件。"""
    async with session.get(
        url, headers=_headers_for(url), allow_redirects=True
    ) as resp:
        if resp.status != 200:
            raise HttpError(f"下载失败 HTTP {resp.status}: {url}")

        # 有 Content-Length 时预判超限，别把整个大文件下完才发现超了
        clen = resp.content_length
        if max_bytes > 0 and clen and clen > max_bytes:
            raise MediaTooLarge(
                f"媒体大小 {clen / 1024 / 1024:.1f}MB 超过上限 {max_bytes / 1024 / 1024:.1f}MB"
            )

        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        path = _temp_dir() / f"{prefix}_{digest}{_guess_ext(url, resp.content_type)}"

        total = 0
        with open(path, "wb") as fh:
            async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                total += len(chunk)
                if max_bytes > 0 and total > max_bytes:
                    fh.close()
                    path.unlink(missing_ok=True)
                    raise MediaTooLarge(
                        f"媒体大小超过上限 {max_bytes / 1024 / 1024:.1f}MB"
                    )
                fh.write(chunk)

        logger.debug(f"[R插件] 流式下载 {total / 1024:.0f}KB -> {path.name}")
        return path


async def download_many(
    urls: list[str],
    *,
    prefix: str = "media",
    max_bytes: int = 0,
    concurrency: int = 6,
    timeout: float = 120.0,
) -> list[Path | None]:
    """并发下载多个媒体，返回与输入等长的本地路径列表。

    一个共享的 ``ClientSession``（连接池复用）+ ``Semaphore`` 限流，
    图集几十张图能并行拉，比串行快一个量级。单条失败不影响其它。

    Args:
        concurrency: 并发上限（连接池也按这个开）。
        max_bytes: 单文件大小上限（0 不限制）。

    Returns:
        与 ``urls`` 等长的列表，成功为本地路径，失败/超限为 ``None``。
    """
    if not urls:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))
    connector = aiohttp.TCPConnector(
        ssl=False, limit=concurrency, limit_per_host=concurrency
    )
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(
        timeout=timeout_obj, connector=connector
    ) as session:

        async def one(url: str) -> Path | None:
            async with sem:
                try:
                    return await _stream_one(
                        session, url, prefix=prefix, max_bytes=max_bytes
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                    logger.warning(f"[R插件] 下载失败 {url[:80]}: {exc}")
                    return None

        results = await asyncio.gather(*(one(u) for u in urls))
        return list(results)
