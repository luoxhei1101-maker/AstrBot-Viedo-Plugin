"""服务器 / Bot 状态采集。

给 ``#服务状态`` 用。所有取值都做了兜底：拿不到就给「—」，绝不抛异常
（一个状态命令不该因为某个接口抽风而失败）。

- 系统负载：``psutil``（AstrBot 官方镜像内置 7.1.3）。psutil 万一缺失就退回
  ``/proc`` + ``os.getloadavg`` 的手工计算。
- Bot 账号：``event.get_self_id()``。
- Bot 昵称：尽量通过 OneBot 的 ``get_login_info`` 拿（aiocqhttp 适配器下是
  ``event.bot.api.call_action``），拿不到就只显示 QQ 号。
- Bot 头像：``https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640``（实测服务器可达，
  约 3 秒，所以调用方要把它和别的耗时操作**并行**起来）。
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import time
from typing import Any

from astrbot.api import logger

AVATAR_API = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"


def _uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
    except Exception:  # noqa: BLE001
        return "—"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天 {h}小时 {m}分"
    if h:
        return f"{h}小时 {m}分"
    return f"{m}分"


def _meminfo() -> tuple[int, int]:
    """(总量, 可用) 字节 —— 不给 psutil 用。"""
    total = avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                if total and avail:
                    break
    except Exception:  # noqa: BLE001
        pass
    return total, avail


def collect_system() -> dict:
    """采集系统指标。返回给面板用的 ``loads`` / ``env_lines``。"""
    loads: list[dict] = []

    try:
        import psutil

        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        try:
            load1, load5, load15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = 0.0
        cores = psutil.cpu_count() or 1
        boot = psutil.boot_time()
        up_secs = time.time() - boot
        d, rem = divmod(int(up_secs), 86400)
        h, rem = divmod(rem, 3600)
        uptime = f"{d}天 {h}小时" if d else f"{h}小时 {rem // 60}分"

        loads = [
            {"label": f"CPU（{cores} 核）", "percent": cpu, "text": f"{cpu:.0f}%"},
            {
                "label": "内存",
                "percent": vm.percent,
                "text": f"{_human(vm.used)} / {_human(vm.total)}　{vm.percent:.0f}%",
            },
            {
                "label": "磁盘 /",
                "percent": du.percent,
                "text": f"{_human(du.used)} / {_human(du.total)}　{du.percent:.0f}%",
            },
            {
                "label": "系统负载（1/5/15 分钟）",
                "percent": min(100.0, load1 / cores * 100) if cores else 0.0,
                "text": f"{load1:.2f} / {load5:.2f} / {load15:.2f}",
            },
        ]
        env = [
            f"运行时长　{uptime}",
            f"内核　{platform.release()}　·　架构　{platform.machine()}",
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][服务状态] psutil 不可用（{exc}），退回 /proc 读取")
        total, avail = _meminfo()
        used = max(0, total - avail)
        pct = (used / total * 100) if total else 0.0
        du = shutil.disk_usage("/")
        loads = [
            {"label": "内存", "percent": pct,
             "text": f"{_human(used)} / {_human(total)}　{pct:.0f}%"},
            {"label": "磁盘 /", "percent": du.percent,
             "text": f"{_human(du.used)} / {_human(du.total)}　{du.percent:.0f}%"},
        ]
        env = [f"运行时长　{_uptime()}"]

    # 运行环境
    import sys

    env.append(f"Python　{sys.version.split()[0]}")
    try:
        import PIL

        env.append(f"Pillow　{PIL.__version__}")
    except Exception:  # noqa: BLE001
        env.append("Pillow　未安装（图片功能会退回文字）")
    try:
        from .media import ffmpeg_available  # type: ignore[attr-defined]

        env.append(f"ffmpeg　{'可用' if ffmpeg_available() else '缺失'}")
    except Exception:  # noqa: BLE001
        pass

    return {"loads": loads, "env_lines": env}


async def fetch_avatar(qq: str, timeout: float = 12.0) -> Any:
    """拉 Bot 头像并转成 PIL Image（失败返回 None）。"""
    if not qq or not qq.isdigit():
        return None
    from .http import get_session

    url = AVATAR_API.format(qq=qq)
    try:
        session = get_session()
        async with session.get(url, timeout=timeout,
                               headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][服务状态] 头像下载失败: {exc}")
        return None
    try:
        from PIL import Image

        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][服务状态] 头像解析失败: {exc}")
        return None


async def fetch_bot_name(event: Any) -> str:
    """尝试通过 OneBot 的 ``get_login_info`` 拿昵称。"""
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call = getattr(api, "call_action", None)
    if not callable(call):
        return ""
    try:
        info = await call("get_login_info")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][服务状态] get_login_info 失败: {exc}")
        return ""
    if isinstance(info, dict):
        data = info.get("data") if "data" in info else info
        if isinstance(data, dict):
            return str(data.get("nickname") or "")
    return ""


def self_id_of(event: Any) -> str:
    for getter in ("get_self_id",):
        fn = getattr(event, getter, None)
        if callable(fn):
            try:
                sid = fn()
            except Exception:  # noqa: BLE001
                continue
            if sid:
                return str(sid)
    return ""
