"""媒体后处理：DASH 音视频合并、转码。

B 站的 DASH 流是把视频轨和音频轨分开传的（各自独立的 m4s），
**直接发视频轨出去是没有声音的**，必须先用 ffmpeg 合起来。

这个模块负责：
1. 并发下载视频轨 / 音频轨
2. 调 ffmpeg 无损封装（``-c copy``，不重新编码，几秒搞定）
3. 失败时明确回报原因，让上层决定是降级还是报错
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

from astrbot.api import logger

from .external import find_tool, run


class MergeError(RuntimeError):
    """合并失败。"""


def _work_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "astrbot_plugin_rconsole" / "merge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ffmpeg_available() -> bool:
    return find_tool("ffmpeg") is not None


async def _download(url: str, dest: Path, *, headers: dict | None = None) -> Path:
    """下载一个流到指定路径。"""
    import aiohttp

    merged_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }
    if headers:
        merged_headers.update(headers)

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300),
        connector=connector,
    ) as session:
        async with session.get(url, headers=merged_headers) as resp:
            if resp.status != 200:
                raise MergeError(f"流下载失败 HTTP {resp.status}: {url[:100]}")
            dest.write_bytes(await resp.read())
    return dest


async def merge_dash(
    video_url: str,
    audio_url: str,
    *,
    tag: str = "bili",
    headers: dict | None = None,
    timeout: float = 300.0,
) -> Path:
    """把 DASH 的视频轨和音频轨合并成一个 mp4。

    Args:
        tag: 文件名前缀，方便排查是哪个平台产生的文件。

    Returns:
        合并后的 mp4 路径。

    Raises:
        MergeError: ffmpeg 不存在、下载失败或合并失败。
    """
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise MergeError("未找到 ffmpeg，无法合并 DASH 音视频（只能发无声音的视频轨）")

    digest = hashlib.md5(f"{video_url}{audio_url}".encode()).hexdigest()[:10]
    work = _work_dir()
    v_path = work / f"{tag}_{digest}.video.m4s"
    a_path = work / f"{tag}_{digest}.audio.m4s"
    out_path = work / f"{tag}_{digest}.mp4"

    # 已经有合并好的结果就直接复用，省一次下载 + 合并
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.debug(f"[R插件][合并] 命中缓存: {out_path}")
        return out_path

    logger.info("[R插件][合并] 开始下载 DASH 音视频轨")
    await asyncio.gather(
        _download(video_url, v_path, headers=headers),
        _download(audio_url, a_path, headers=headers),
    )
    v_mb = v_path.stat().st_size / 1024 / 1024
    a_mb = a_path.stat().st_size / 1024 / 1024
    logger.info(f"[R插件][合并] 下载完成 视频 {v_mb:.1f}MB / 音频 {a_mb:.1f}MB，调用 ffmpeg")

    result = await run(
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(v_path),
        "-i", str(a_path),
        "-c", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        str(out_path),
        timeout=timeout,
    )

    if not result.ok or not out_path.exists():
        raise MergeError(f"ffmpeg 合并失败: {result.tail}")

    out_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"[R插件][合并] 完成 {out_mb:.1f}MB -> {out_path.name}")

    # 清掉中间产物，只留 mp4
    for tmp in (v_path, a_path):
        try:
            tmp.unlink()
        except OSError:
            pass

    return out_path
