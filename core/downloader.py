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

    注意：这个函数走 ``download_bytes``（一次性读进内存），**不做 Content-Type
    校验**——抖音签名 CDN 的「软失败」（200 + text/html 错误页）挡不住。
    需要这个保护时用 ``download_many`` / ``download_many_candidates``。
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


async def download_media_candidates(
    urls: list[str],
    *,
    prefix: str = "media",
    max_bytes: int = 0,
    timeout: float = 120.0,
    require_media: bool = True,
) -> Path:
    """按顺序尝试一组候选 URL，返回**第一个真正下载成功**的本地路径。

    与 ``download_media`` 的区别：带 Content-Type / 大小校验，能挡住
    「200 + text/html 错误页」这种软失败。全部候选失败时抛最后一个错误。

    抖音图集的动图视频轨就是典型场景：``play_addr`` 的 url_list 同样有多个
    节点，直接取第一个常 403。
    """
    if not urls:
        raise HttpError("没有可下载的候选地址")

    last_error: Exception | None = None
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), connector=aiohttp.TCPConnector(ssl=False)
    ) as session:
        for url in urls:
            try:
                return await _stream_one(
                    session,
                    url,
                    prefix=prefix,
                    max_bytes=max_bytes,
                    expect_media=require_media,
                )
            except Exception as exc:  # noqa: BLE001 - 换下一个候选
                last_error = exc
                logger.debug(f"[R插件] 媒体候选失败 {url[:60]}: {exc}")
                continue

    raise HttpError(f"全部 {len(urls)} 个候选均下载失败：{last_error}")


async def _stream_one(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefix: str,
    max_bytes: int,
    expect_media: bool = False,
) -> Path:
    """用共享 session 流式下载单个文件。

    Args:
        expect_media: 为 True 时要求响应确实是媒体（见下方「软失败」说明）。
            **下载图片/视频时必须开**——抖音 CDN 拿不到图时不会给 4xx/5xx 之外的
            信号，而是回一个 200/206 + ``text/html`` 的错误页（实测 238 字节），
            不校验的话这段 HTML 会被当成图片写盘、发出去是个破图。
    """
    async with session.get(
        url, headers=_headers_for(url), allow_redirects=True
    ) as resp:
        if resp.status != 200:
            raise HttpError(f"下载失败 HTTP {resp.status}: {url}")

        # 抖音签名 CDN 的「软失败」：状态码正常但内容是 HTML 错误页。
        # 实测特征：Content-Type: text/html，正文 238 字节。
        # 必须在写盘**之前**拦掉，否则会生成一个假图片文件。
        if expect_media:
            ctype = (resp.content_type or "").lower()
            if ctype and not (
                ctype.startswith("image/") or ctype.startswith("video/")
                or ctype.startswith("audio/") or ctype == "application/octet-stream"
            ):
                raise HttpError(
                    f"下载到非媒体内容（Content-Type: {ctype}）: {url}"
                )

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

        # 内容太小的「成功」几乎都是错误页（没有正常图片/视频只有几百字节）。
        # 阈值取 1KB：最小的正常缩略图也远大于这个值。
        if expect_media and total < 1024:
            path.unlink(missing_ok=True)
            raise HttpError(f"下载内容过小（{total} 字节），疑似错误页: {url}")

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
                        session, url, prefix=prefix, max_bytes=max_bytes,
                        expect_media=True,
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                    logger.warning(f"[R插件] 下载失败 {url[:80]}: {exc}")
                    return None

        results = await asyncio.gather(*(one(u) for u in urls))
        return list(results)


async def download_many_candidates(
    candidates: list[list[str]],
    *,
    prefix: str = "media",
    max_bytes: int = 0,
    concurrency: int = 6,
    timeout: float = 120.0,
) -> list[Path | None]:
    """并发下载，每项是一组候选 URL，逐个尝试直到成功。

    抖音图集每张图的 ``url_list`` 有多个 CDN 节点（p3-pc-sign / p9-pc-sign …），
    403 出现在哪个候选是**每张图固定、但不同图不同**（实测同一张图 3 轮探测
    结果完全一致，见 ``douyin_ssr.rank_image_candidates``）。所以这里逐个回退是
    有效的——但**候选顺序必须已经排好「原图优先」**，否则会拿到 ``.webp``
    压缩预览。排序由 ``rank_image_candidates`` 在解析层完成。

    每项下载都带 ``expect_media=True``：抖音签名 CDN 的失败不是 4xx/5xx，
    而是 **200 + text/html 的 238 字节错误页**，不校验就会被当成图片发出去。
    """
    if not candidates:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))
    connector = aiohttp.TCPConnector(
        ssl=False, limit=concurrency, limit_per_host=concurrency
    )
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(
        timeout=timeout_obj, connector=connector
    ) as session:

        async def one(cands: list[str]) -> Path | None:
            async with sem:
                last_err = ""
                for url in cands:
                    try:
                        return await _stream_one(
                            session, url, prefix=prefix, max_bytes=max_bytes,
                            expect_media=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - 换下一个候选
                        last_err = f"{type(exc).__name__}: {exc}"
                        logger.debug(
                            f"[R插件] 候选下载失败 {url[:60]}: {exc}"
                        )
                        continue
                # 全部候选都失败：这条是**可诊断的关键信息**（几个候选、什么错），
                # 用 warning 级别打出来——不然用户反馈「图没发出来」时，
                # 日志里只剩一条「某张下载失败」，看不出是候选全废还是签名过期
                logger.warning(
                    f"[R插件] 图片候选全部失败（{len(cands)} 个候选），"
                    f"末次错误: {last_err}"
                )
                return None

        results = await asyncio.gather(*(one(c) for c in candidates))
        return list(results)
