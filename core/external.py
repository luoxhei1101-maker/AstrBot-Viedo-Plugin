"""外部命令行工具的探测与调用封装。

原版用 ``exec`` / ``execSync`` 直接调 ffmpeg、yt-dlp、BBDown、aria2、tdl。
移植后保留这个能力，但做了三件事让它更可控：

1. **启动时探测一次**，结果缓存下来，插件启动日志里直接告诉你缺什么；
2. **统一超时**，不让某个卡死的命令把事件循环旁边挂着的任务拖死；
3. **统一错误信息**，失败时把 stderr 尾部带出来，而不是只报个退出码。

注意：这些命令都是阻塞式的，调用时用 ``asyncio.to_thread`` 丢到线程里，
不要直接在协程里 await——AstrBot 有事件循环看门狗，阻塞超过 30 秒会被记录。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass

from astrbot.api import logger

# 原版用到的全部外部命令，以及它们各自的用途
TOOL_USAGE: dict[str, str] = {
    "ffmpeg": "音视频合并 / 转码 / m3u8 拼接",
    "ffprobe": "读取媒体元信息（时长、编码）",
    "yt-dlp": "YouTube 等站点的下载",
    "BBDown": "B 站高画质下载（原版可选）",
    "aria2c": "多线程下载器（原版可选）",
    "tdl": "Telegram 文件下载",
}

# 探测结果缓存：name -> 绝对路径 或 None
_TOOL_CACHE: dict[str, str | None] = {}


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def tail(self) -> str:
        """错误信息的尾部，够定位问题就行，不用把整屏日志刷出来。"""
        text = (self.stderr or self.stdout or "").strip()
        return text[-600:] if len(text) > 600 else text


def find_tool(name: str, refresh: bool = False) -> str | None:
    """查找外部命令，结果缓存。"""
    if not refresh and name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    path = shutil.which(name)
    _TOOL_CACHE[name] = path
    return path


def probe_all(refresh: bool = False) -> dict[str, str | None]:
    """探测所有原版用到的外部命令。"""
    return {name: find_tool(name, refresh) for name in TOOL_USAGE}


def describe_environment() -> str:
    """生成一段人类可读的环境报告，插件启动时打日志用。"""
    found: list[str] = []
    missing: list[str] = []
    for name, usage in TOOL_USAGE.items():
        if find_tool(name):
            found.append(name)
        else:
            missing.append(f"{name}({usage})")

    parts = []
    if found:
        parts.append("已安装：" + ", ".join(found))
    if missing:
        parts.append("缺失：" + ", ".join(missing))
    return " | ".join(parts) if parts else "未探测到任何外部工具"


async def run(
    *args: str,
    timeout: float = 180.0,
    cwd: str | None = None,
) -> CommandResult:
    """异步执行一条外部命令。

    用 ``asyncio.to_thread`` 包住 ``subprocess.run``，避免阻塞事件循环。
    """

    def _invoke() -> CommandResult:
        try:
            proc = subprocess.run(  # noqa: S603 - 命令来自插件内部常量，不是用户输入
                list(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
                cwd=cwd,
                check=False,
            )
            return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            return CommandResult(-1, "", f"命令超时（{timeout}s）: {' '.join(args)}: {exc}")
        except FileNotFoundError:
            return CommandResult(-2, "", f"命令不存在: {args[0]}")

    result = await asyncio.to_thread(_invoke)
    if not result.ok:
        logger.warning(f"[R插件][外部命令] {' '.join(args[:3])} 失败: {result.tail}")
    return result


async def tool_version(name: str) -> str:
    """取外部工具的版本号，用于自检日志。"""
    path = find_tool(name)
    if not path:
        return "未安装"
    # 不同工具的版本参数不一样，逐个试
    for flag in ("-version", "--version", "-V"):
        result = await run(path, flag, timeout=12.0)
        if result.ok:
            first_line = (result.stdout or result.stderr).strip().splitlines()
            if first_line:
                return first_line[0][:80]
    return f"已安装（{path}）"
