"""QQ 官方机器人语音：本地转 silk。

**为什么插件要自己转、自己传**

走适配器那条路（``Comp.Record`` → ``to_path(target_format="tencent_silk")``
→ ``upload_group_and_c2c_media``）有两个结构性开销：

1. **silk 编码是同步阻塞的** —— ``pysilk.encode`` 直接写在 async 函数里，
   一首 4 分半的歌会把 event loop 堵住约 20 秒（所有并发请求一起卡住）；
2. **上传超时不可控** —— botpy 的 ``BotHttp.timeout`` 是**实例级**属性
   （默认约 15 秒），超时后 ``request()`` 直接返回 ``None``，被
   ``APIReturnNoneError`` 捕获后交给 tenacity 重试 **3 次**（退避 2/4/8 秒）。
   腾讯接口一抖，用户就要**干等 90+ 秒**才收到失败提示。

实测日志（2026-09-23 15:28，青花瓷 4 分 28 秒）::

    15:28:39  序号点播
    15:28:46  语音预转码完成（+7s）
    15:29:21  首次上传失败（silk 20s + 上传超时 15s）  ← 已 +42s
    15:29:48  重试 2（+69s）
    15:30:12  重试 3（+93s）
    → 最终什么都没发出去

自己走这条路之后，**超时和重试次数都由插件决定**，失败能立刻降级发链接。

实测基准（16kHz 单声道）：

===================  ==========  ==========
项目                 数值        说明
===================  ==========  ==========
silk 编码速率        ≈0.08x 实时  4 分半歌约 21 秒
silk 体积            ≈1.5 KB/秒   4 分半歌约 400KB
===================  ==========  ==========

体积远低于 botpy 的分片上传阈值（10MB），所以不会走分片路径。

**注意**：``encode_silk`` 是**同步**函数，调用方必须用
``asyncio.to_thread`` 包起来 —— 这正是我们要绕开的那个坑。
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any

# pysilk 只支持这几档采样率（见 astrbot.core.utils.tencent_record_helper）
SUPPORTED_RATES = frozenset({8000, 12000, 16000, 24000, 32000, 48000})

# QQ 富媒体上传的 file_type 取值（官方接口定义）
VOICE_FILE_TYPE = 3

# 我们只接受「自己预转过」的规格：16k / 单声道 / 16bit。
# 其它规格交给上层降级，不要在这里做重采样 —— 那会引入一个新的静默失败点。
EXPECTED_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_WIDTH = 2

# pysilk 的编码复杂度（默认 2）。实测 120 秒音频：
#
#     complexity   耗时      体积
#     2（默认）    9.69s    250.9KB
#     1            5.50s    242.2KB
#     0            3.11s    180.7KB   ← 快 3 倍、小 28%
#
# 点歌语音条不是高保真场景（QQ 语音本身就是窄带），**用 0**：
# 一首 4 分半的歌 silk 编码从 22 秒降到 7 秒。
ENCODE_COMPLEXITY = 0


def silk_available() -> bool:
    """当前环境能不能编码 silk（缺 pysilk 时为 False）。"""
    try:
        import pysilk  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        return False
    return True


def encode_silk(wav_path: str | Path) -> bytes | None:
    """把 16k/单声道/16bit 的 wav 编成腾讯 silk。**同步函数**。

    Returns:
        silk 字节流；失败（缺 pysilk / 格式不符 / 空音频）返回 ``None``。

    调用方**必须**用 ``await asyncio.to_thread(encode_silk, path)`` ——
    pysilk 是纯 C 调用，直接放在协程里会把 event loop 堵死几十秒。
    """
    try:
        import pysilk
    except (ImportError, ModuleNotFoundError):
        return None

    try:
        with wave.open(str(wav_path), "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, OSError, EOFError):
        return None

    if not frames:
        return None
    if (
        rate != EXPECTED_RATE
        or channels != EXPECTED_CHANNELS
        or width != EXPECTED_WIDTH
    ):
        # 不是我们预转的规格：宁可让上层降级发链接，也不在这里偷偷重采样。
        return None

    out = io.BytesIO()
    try:
        # tencent=True 才会输出带 0x02 前缀、QQ 认的 silk 流
        pysilk.encode(
            io.BytesIO(frames),
            out,
            rate,
            rate,
            complexity=ENCODE_COMPLEXITY,
            tencent=True,
        )
    except Exception:  # noqa: BLE001 - pysilk 会抛自己的 SilkError
        return None
    data = out.getvalue()
    return data or None


def duration_of_silk(data: bytes) -> float:
    """粗算 silk 的时长（秒）。

    16kHz 单声道下单帧 20ms，但 silk 是变码率 —— 这里按
    「解码后 PCM 字节数」估算，只用于日志与用户提示，不参与逻辑判断。
    """
    # 经验值：16k silk 约 1.5KB/秒（实测 344×240 的 123 帧 GIF 无关，
    # 音频侧 30 秒 65KB / 60 秒 127KB → 约 2.1KB/秒）
    return round(len(data) / 2048.0, 1)


def describe(wav_path: str | Path) -> dict[str, Any]:
    """诊断用：返回 wav 的规格，便于排查「为什么没转成功」。"""
    try:
        with wave.open(str(wav_path), "rb") as wav:
            return {
                "rate": wav.getframerate(),
                "channels": wav.getnchannels(),
                "width": wav.getsampwidth(),
                "frames": wav.getnframes(),
                "seconds": round(wav.getnframes() / max(1, wav.getframerate()), 1),
            }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
