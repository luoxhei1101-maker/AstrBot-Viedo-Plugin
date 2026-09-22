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


def is_animated_image(path: str | Path) -> bool:
    """判断一个图片文件是不是**动图**（GIF / 动图 WebP）。

    为什么要看文件本身：评论里的「动图」在接口层**没有任何标记** ——
    ``image_list`` 的字段和静态图完全一样（实测扫了 28 个作品 / 955 条评论，
    连 ``video_list`` 都全是 None），所以只能下下来看内容。

    Pillow 不可用或读不动这个文件时一律返回 False：当作静态图发，
    最坏是「动图不动」，不会让整条流程失败。
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(path) as im:
            frames = int(getattr(im, "n_frames", 1))
            return bool(getattr(im, "is_animated", False)) and frames > 1
    except Exception:  # noqa: BLE001
        return False


async def animated_to_mp4(src: str | Path, *, timeout: float = 120.0) -> Path | None:
    """把动图转成 mp4；失败返回 None（由调用方决定降级）。

    为什么转 mp4：QQ 里「动图」按**视频**发才稳 —— 直接发 GIF，部分客户端
    会把它压成静态首帧，用户就看不到动效了。

    几个必要的参数：

    - ``scale=trunc(iw/2)*2:trunc(ih/2)*2``：h264 要求宽高为偶数，
      GIF 常见奇数尺寸，不补齐会被 ffmpeg 直接拒绝。
    - ``-pix_fmt yuv420p``：不加的话某些播放器 / 协议端放不出来。
    - ``-movflags +faststart``：把 moov 挪到文件头，发送端能边下边播。
    - ``-an``：动图本来就没音轨，显式声明，免得 ffmpeg 塞一条空音轨进去。
    """
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None

    src = Path(src)
    out = src.with_name(f"{src.stem}_anim.mp4")
    result = await run(
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(src),
        "-an",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        str(out),
        timeout=timeout,
    )
    if not result.ok or not out.exists() or out.stat().st_size == 0:
        logger.debug(f"[R插件][动图] 转 mp4 失败: {getattr(result, 'tail', '')}")
        return None
    return out


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
