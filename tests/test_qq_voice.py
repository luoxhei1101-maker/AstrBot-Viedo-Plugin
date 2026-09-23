"""官机语音（``core/qq_voice.py`` + ``main._send_qq_voice``）回归测试。

**这个文件要守住的核心不变量**：语音慢/卡死的两个结构性原因不能再回来 ——

1. **silk 编码必须在 ``asyncio.to_thread`` 里跑**。pysilk 是同步 C 调用，
   4 分半的歌要几秒到几十秒；放协程里会堵死整个 event loop
   （所有并发消息一起卡，而且表面看代码"没毛病"）。
2. **上传超时必须由插件自己控制**。适配器用 botpy 的实例级超时（约 15s）
   + tenacity 重试 3 次，腾讯接口一抖用户就要干等 90+ 秒。我们改成
   短超时 + 失败立刻降级发链接。

顺带锁住 ``complexity=0`` 这个提速开关 —— 实测它把编码耗时砍到 1/3
（120 秒音频 9.69s → 3.11s），很容易被"顺手改回默认"。

跑法（要在装了依赖的虚拟环境里；pysilk 缺失时 B 段会跳过）::

    python tests/test_qq_voice.py
"""

from __future__ import annotations

import pathlib
import struct
import sys
import tempfile
import wave

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from astrbot_plugin_rconsole.core import qq_voice as qv  # noqa: E402

FAILED: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}\n       期望: {want!r}\n       实际: {got!r}")
        FAILED.append(label)


def check_true(label: str, cond, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}" + (f"\n       {detail}" if detail else ""))
        FAILED.append(label)


def _make_wav(
    path: pathlib.Path,
    seconds: float,
    *,
    rate: int = 16000,
    channels: int = 1,
    width: int = 2,
    silent: bool = False,
) -> pathlib.Path:
    """手写一个 wav（不依赖 ffmpeg，测试在任何机器上都能跑）。"""
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        if silent or width == 1:
            val = 0
        else:
            # 440Hz 正弦，幅度留一半避免削波
            import math

            val = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
        for _ in range(channels):
            frames += struct.pack("<h", val)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


# ==========================================================================
# A) 环境探测
# ==========================================================================
def part_a_available() -> None:
    print("\n[A] 环境探测")
    check_true("silk_available() 返回 bool", isinstance(qv.silk_available(), bool))
    check_true(
        "常量：只接受 16k / 单声道 / 16bit",
        qv.EXPECTED_RATE == 16000
        and qv.EXPECTED_CHANNELS == 1
        and qv.EXPECTED_WIDTH == 2,
        f"{qv.EXPECTED_RATE}/{qv.EXPECTED_CHANNELS}/{qv.EXPECTED_WIDTH}",
    )
    check("file_type：语音 = 3", qv.VOICE_FILE_TYPE, 3)
    check_true(
        "16k 在 pysilk 支持的采样率里（否则框架会触发重采样）",
        16000 in qv.SUPPORTED_RATES,
    )


# ==========================================================================
# B) silk 编码行为（真跑）
# ==========================================================================
def part_b_encode() -> None:
    print("\n[B] silk 编码")
    if not qv.silk_available():
        print("  ⏭ 跳过：本地没有 pysilk（容器里才有）")
        return

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)

        good = _make_wav(d / "ok.wav", 1.0)
        data = qv.encode_silk(good)
        check_true("16k 单声道 wav → 出 silk", bool(data), "返回了 None")
        if data:
            check_true("silk 非空", len(data) > 100, f"{len(data)} 字节")
            # tencent=True 的输出带 0x02 前缀，QQ 靠它识别
            check("silk 以 0x02 开头（tencent 标志）", data[:1], b"\x02")

        # ---- 规格不符必须拒绝，而不是偷偷重采样 ----
        check(
            "44.1k 被拒（不该静默重采样）",
            qv.encode_silk(_make_wav(d / "44k.wav", 0.3, rate=44100)),
            None,
        )
        check(
            "立体声被拒",
            qv.encode_silk(_make_wav(d / "st.wav", 0.3, channels=2)),
            None,
        )
        check(
            "8bit 被拒",
            qv.encode_silk(_make_wav(d / "8b.wav", 0.3, width=1)),
            None,
        )
        check(
            "空音频被拒",
            qv.encode_silk(_make_wav(d / "empty.wav", 0.0)),
            None,
        )
        check("不存在的文件返回 None", qv.encode_silk(d / "nope.wav"), None)
        check(
            "非 wav 文件返回 None",
            qv.encode_silk(_ROOT / "metadata.yaml"),
            None,
        )

        # ---- describe 只用于诊断，不该抛 ----
        info = qv.describe(good)
        check("describe 能读到规格", (info.get("rate"), info.get("channels")), (16000, 1))
        check_true("describe 对坏文件不抛", "error" in qv.describe(d / "nope.wav"))

        # ---- 时长估算（只用于日志/提示）----
        check_true(
            "duration_of_silk 返回正数",
            data is not None and qv.duration_of_silk(data) > 0,
        )


# ==========================================================================
# C) 提速开关不能被改回默认
# ==========================================================================
def part_c_perf_guard() -> None:
    print("\n[C] 提速开关")
    check("ENCODE_COMPLEXITY == 0（快 3 倍）", qv.ENCODE_COMPLEXITY, 0)
    check_true(
        "encode_silk 是**同步** def（调用方才能 to_thread）",
        "async def encode_silk" not in (_ROOT / "core" / "qq_voice.py").read_text("utf-8"),
        "它变回 async 的话，main 里的 to_thread 会拿到 coroutine 直接失效",
    )


# ==========================================================================
# D) main.py 源码不变量
# ==========================================================================
def part_d_main_guard() -> None:
    print("\n[D] main.py 源码不变量")
    src = (_ROOT / "main.py").read_text(encoding="utf-8")

    check_true(
        "silk 编码走 to_thread（不堵 event loop）",
        "await asyncio.to_thread(qq_encode_silk" in src,
        "pysilk 是同步 C 调用，直接 await 会把整个 loop 卡住",
    )
    check_true("有 _send_qq_voice", "async def _send_qq_voice" in src)
    check_true("有 _upload_qq_media（自控超时版）", "async def _upload_qq_media" in src)
    check_true(
        "上传前临时改短 http.timeout、用完恢复",
        "http.timeout = timeout" in src and "http.timeout = saved" in src,
        "botpy 的 timeout 是实例级属性，没有 per-request 参数",
    )
    check_true(
        "上传用 file_type 常量而不是硬编码 3",
        "QQ_VOICE_FILE_TYPE" in src,
    )
    check_true(
        "语音上传失败会降级（不是静默）",
        "语音上传超时" in src,
    )
    check_true(
        "只对「秒失败」重试（耗满超时的说明被腾讯排队，重试只会更慢）",
        "_QQ_UPLOAD_FAST_FAIL" in src and "elapsed < _QQ_UPLOAD_FAST_FAIL" in src,
    )
    check_true(
        "上传超时用常量且 >= 20s（实测冷却后首次 8.3s，要留余量）",
        "_QQ_UPLOAD_TIMEOUT" in src and "= 25.0" in src,
        "12 秒只留 44% 余量，稍有抖动就误杀 —— 实测踩过",
    )
    check_true(
        "官机分支同时要求「预转成功」和「有 pysilk」",
        'caps.key == "qqofficial" and small is not None and qq_silk_available()' in src,
        "非 16k 单声道的输入不在 encode_silk 的处理范围，硬走只会白折腾",
    )
    check_true(
        "发语音用 msg_type=7 + media.file_info",
        '"msg_type": 7' in src and '"media": {"file_info": file_info}' in src,
    )
    check_true(
        "带上 content=None（适配器发富媒体时一定有这个字段）",
        '"content": None' in src,
    )

    # 确认这条路径真的接进了点歌流程
    seg = src.split("async def _music_render_voice")[1].split("async def ")[0]
    check_true("_music_render_voice 里接了 _send_qq_voice", "_send_qq_voice(event, path)" in seg)


def main() -> int:
    part_a_available()
    part_b_encode()
    part_c_perf_guard()
    part_d_main_guard()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
