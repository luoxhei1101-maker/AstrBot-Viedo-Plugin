"""本地图片 → 公网直链。

为什么需要它
------------
QQ 官方机器人（``qq_official``）发本地图时，AstrBot 适配器**只有 base64 一条路**：

    # qqofficial_message_event.py
    if image_is_local:
        image_base64 = resolved.to_base64()        # 本地文件 -> base64
    else:
        image_base64 = await resolver.to_base64()  # URL 也先下载再 base64
    ...
    payload = {"file_data": image_base64, ...}     # 根本没有 url 字段

对「境外机器 → 腾讯境内 API」这条链路，把文件整个传上去是最慢的一环。实测：

    97KB  图  base64 上传  7.39s / 14.88s
    250KB 图  base64 上传  更久
    ↓ 改走 url 上传后
    784KB 图  url 上传     3.47s
    250KB 图  url 上传     1.64s

腾讯的富媒体接口本身支持 ``{"file_type": 1, "url": "..."}`` —— 平台**自己去下载**，
我们一个字节都不用传。所以只要把本地文件变成一个**公网可访问的 URL**，
就能走这条快路。

做法
----
把字节写进一个静态目录，由用户自己的 HTTP 服务（nginx / python -m http.server /
任意静态托管）暴露成 URL。目录默认取 AstrBot 数据目录下的 ``rconsole_pub`` ——
官方 Docker 镜像里 ``/AstrBot/data`` 通常已经挂到宿主机，方便 nginx 直接指过去。

⚠️ 这是**可选功能**：``localPubBaseUrl`` 留空就完全关闭，一切照旧走 base64。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from astrbot.api import logger

#: 落盘目录（容器内路径）。官方镜像的数据目录是 ``/AstrBot/data``，
#: 一般都挂载到了宿主机，nginx 可以直接指过去。
DEFAULT_DIR = "/AstrBot/data/rconsole_pub"

#: 文件保留时长（秒）。太短可能被腾讯下载前就删了，太长目录会涨。
DEFAULT_TTL = 600

#: 只允许这几种后缀，避免被塞进奇怪的扩展名让静态服务执行
_SAFE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                  ".mp4", ".mp3", ".silk", ".bin"}

#: 单文件上限（字节）。太大就别走这条路了（也避免撑爆磁盘）。
MAX_BYTES = 32 * 1024 * 1024


def normalize_base(base_url: str) -> str:
    """把用户填的 base 归一化：去空白、去尾部斜杠。

    ``http://1.2.3.4:18900/`` 和 ``http://1.2.3.4:18900`` 等价。
    """
    return (base_url or "").strip().rstrip("/")


def safe_suffix(suffix: str) -> str:
    """只接受白名单后缀，其余一律当 ``.bin``。"""
    s = (suffix or "").strip().lower()
    if not s:
        return ".bin"
    if not s.startswith("."):
        s = "." + s
    return s if s in _SAFE_SUFFIXES else ".bin"


def content_name(data: bytes, suffix: str = ".png") -> str:
    """按内容哈希生成文件名 —— 同样的图只落一份，天然去重。"""
    digest = hashlib.sha256(data).hexdigest()[:24]
    return digest + safe_suffix(suffix)


def publish_dir(directory: str = "") -> Path:
    return Path((directory or "").strip() or DEFAULT_DIR)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # 原子替换：腾讯永远读不到半个文件


async def publish(
    data: bytes,
    *,
    suffix: str = ".png",
    base_url: str = "",
    directory: str = "",
    ttl: int = DEFAULT_TTL,
) -> str | None:
    """把字节发布成公网 URL，失败返回 ``None``（调用方要能降级）。

    :param base_url: 用户配置的公网前缀；**空串直接返回 None**（功能关闭）。
    :param directory: 落盘目录，空则用 :data:`DEFAULT_DIR`。
    :param ttl: 顺手清理超过这个时长的旧文件。``<=0`` 表示不清理。
    """
    base = normalize_base(base_url)
    if not base or not data:
        return None
    if len(data) > MAX_BYTES:
        logger.info(
            f"[R插件][本地直链] 文件 {len(data) // 1024}KB 超过上限 "
            f"{MAX_BYTES // 1024 // 1024}MB，不走直链"
        )
        return None

    name = content_name(data, suffix)
    path = publish_dir(directory) / name
    try:
        # 已存在且大小一致就不重写（图集里同一张图重复出现时省一次 IO）
        if not (path.exists() and path.stat().st_size == len(data)):
            await asyncio.to_thread(_write, path, data)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[R插件][本地直链] 写入失败（{type(exc).__name__}: {exc}），退回 base64")
        return None

    if ttl and ttl > 0:
        try:
            await asyncio.to_thread(cleanup, directory, ttl)
        except Exception:  # noqa: BLE001
            pass  # 清理失败不该影响这次发送

    return f"{base}/{name}"


def cleanup(directory: str = "", ttl: int = DEFAULT_TTL) -> int:
    """删掉超过 ``ttl`` 秒没被碰过的文件，返回删除个数。

    用 ``mtime`` 判断；**已经存在**的文件在 :func:`publish` 里不会被重写，
    所以热图会一直留到不再被使用为止。
    """
    d = publish_dir(directory)
    if not d.is_dir():
        return 0
    deadline = time.time() - max(1, ttl)
    removed = 0
    for f in d.iterdir():
        try:
            if not f.is_file():
                continue
            if f.stat().st_mtime < deadline:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
