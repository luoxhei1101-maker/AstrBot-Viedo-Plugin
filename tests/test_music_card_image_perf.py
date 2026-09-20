"""点歌列表图「拼图」性能回归测试（v1.6.5）。

**为什么要有这个测试**：用户反馈点歌出图慢。实测拆解（服务器，10 首 / 10 张封面）
发现瓶颈**不止一处**，而且最反直觉的那处在 event loop：

| 环节 | 改前 | 改后 | 说明 |
|---|---|---|---|
| 封面下载（10 张 gather） | 4778 ms | ~300 ms 起 | 见下 |
| 其中：``resize(320→108, LANCZOS)`` | 3380 ms / 10 张 | ~8 ms | 图已是目标尺寸时是空操作 |
| PNG 编码（``optimize=True``） | 610 ms | 292 ms | 只换来 199→202 KB |
| **端到端** | **4704 ms** | **~900 ms** | 网易云；QQ音乐 ~1200 → ~600 ms |

四个必须锁住的不变量：

1. **封面 URL 要改写成平台的小图地址。** 网易云的图片接口支持
   ``?param=宽y高``，不改写就会下原图再本地缩小 —— 实测原图 545 ms / 3.9 KB
   vs 小图 204 ms / 2.8 KB，更亏的是那步 LANCZOS 要 338 ms/张。
2. **解码必须在 ``asyncio.to_thread`` 里。** 之前是同步写在协程里的，
   会把 event loop 堵死 —— 结果是 ``gather`` 4778 ms ≈ 串行 5198 ms，
   **并发形同虚设**。这条是最容易在后续重构里退化掉的。
3. **绘制 + 编码也要走线程池**（800×1696 的 2 倍图，光 PNG 编码就几百毫秒）。
4. **别开 ``optimize=True``**：多花 170~320 ms 只省 1.5% 体积。

跑法::

    python tests/test_music_card_image_perf.py
"""

from __future__ import annotations

import asyncio
import io
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))

if "astrbot" not in sys.modules:
    _logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api = types.ModuleType("astrbot.api")
    _api.logger = _logger
    _api.AstrBotConfig = dict
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

_FAILED: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}\n       期望: {want!r}\n       实际: {got!r}")
        _FAILED.append(label)


def check_true(label: str, cond: bool) -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}")
        _FAILED.append(label)


# ==========================================================================
# A) 缩略图 URL 改写
# ==========================================================================

NETEASE = "http://p3.music.126.net/0ju8ET1ApZSXfWacc4w49w==/109951169484091680.jpg"
QQ = "https://y.gtimg.cn/music/photo_new/T062R150x150M000003UyhCm02MlwN.jpg"


def part_a_thumb_url() -> None:
    print("\n[A] _thumb_url —— 下小图而不是原图")
    from astrbot_plugin_rconsole.core.music_card_image import _thumb_url

    check(
        "网易云：加 ?param=WyH（关键）",
        _thumb_url(NETEASE, 108),
        NETEASE + "?param=108y108",
    )
    check(
        "网易云：URL 已带 query 时改用 & 连接",
        _thumb_url(NETEASE + "?from=tag", 108),
        NETEASE + "?from=tag&param=108y108",
    )
    check_true(
        "网易云：尺寸跟着 cover_size 走",
        "?param=216y216" in _thumb_url(NETEASE, 216),
    )
    check("QQ音乐：本来已是 R150x150，不该动", _thumb_url(QQ, 108), QQ)
    check("已有 param 的不重复加", _thumb_url(NETEASE + "?param=50y50", 108),
          NETEASE + "?param=50y50")
    check("空 URL 原样返回", _thumb_url("", 108), "")


# ==========================================================================
# B) 圆角遮罩缓存
# ==========================================================================

def part_b_mask_cache() -> None:
    print("\n[B] _rounded_mask —— 10 张封面共用一个遮罩")
    try:
        from astrbot_plugin_rconsole.core.music_card_image import _rounded_mask
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 跳过（Pillow 不可用）: {exc}")
        return
    m1 = _rounded_mask(108, 14)
    m2 = _rounded_mask(108, 14)
    check_true("同参数返回同一对象（缓存生效）", m1 is m2)
    m3 = _rounded_mask(108, 20)
    check_true("不同圆角是不同对象", m1 is not m3)


# ==========================================================================
# C) 封面缓存（命中时不该碰网络）
# ==========================================================================

def part_c_cover_cache() -> None:
    print("\n[C] _download_cover —— 缓存命中时不发请求")
    try:
        from PIL import Image
    except ImportError:
        print("  ⚠️ 跳过（无 Pillow）")
        return

    import astrbot_plugin_rconsole.core.http as http_mod
    from astrbot_plugin_rconsole.core import music_card_image as mci

    buf = io.BytesIO()
    Image.new("RGB", (400, 400), (180, 30, 30)).save(buf, format="PNG")
    payload = buf.getvalue()

    calls: list[str] = []

    class FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return payload

    class FakeSession:
        def get(self, url, timeout=None):
            calls.append(url)
            return FakeResp()

    orig = http_mod.get_session
    http_mod.get_session = lambda: FakeSession()
    mci._cover_cache.clear()
    try:
        url = "http://p3.music.126.net/aaa/bbb.jpg"
        img1 = asyncio.run(mci._download_cover(url, 108))
        n_after_first = len(calls)
        img2 = asyncio.run(mci._download_cover(url, 108))
        n_after_second = len(calls)
    finally:
        http_mod.get_session = orig
        mci._cover_cache.clear()

    check_true("首次下载成功", img1 is not None)
    check("首次发起了 1 次请求", n_after_first, 1)
    check_true(
        "第二次命中缓存，没有多发请求（关键）",
        n_after_second == n_after_first,
    )
    check_true("第二次拿到的是同一对象", img2 is img1)
    check_true("请求的是缩小后的 URL", calls and "param=108y108" in calls[0])


# ==========================================================================
# D) 解码语义
# ==========================================================================

def part_d_decode() -> None:
    print("\n[D] _decode_cover —— 尺寸语义")
    try:
        from PIL import Image
    except ImportError:
        print("  ⚠️ 跳过（无 Pillow）")
        return
    from astrbot_plugin_rconsole.core.music_card_image import _decode_cover

    def png(w, h):
        b = io.BytesIO()
        Image.new("RGB", (w, h), (10, 120, 200)).save(b, format="PNG")
        return b.getvalue()

    check("大图缩到目标尺寸", _decode_cover(png(400, 400), 108).size, (108, 108))
    check("正好目标尺寸时不变", _decode_cover(png(108, 108), 108).size, (108, 108))
    # 小图也要统一到目标尺寸：paste 时用的圆角遮罩是 cover_size×cover_size，
    # 图比遮罩小会导致尺寸不匹配。放大糊掉总好过贴图错位。
    check("小图也统一到目标尺寸", _decode_cover(png(64, 64), 108).size, (108, 108))
    check("非方形图统一成方形", _decode_cover(png(400, 200), 108).size, (108, 108))


# ==========================================================================
# E) 源码不变量（防回退）
# ==========================================================================

def part_e_source_guard() -> None:
    print("\n[E] 源码不变量 —— 防止改回同步阻塞")
    src = (_ROOT / "core" / "music_card_image.py").read_text(encoding="utf-8")

    check_true(
        "_download_cover 里解码走 asyncio.to_thread（关键）",
        "await asyncio.to_thread(_decode_cover" in src,
    )
    check_true(
        "绘制走 asyncio.to_thread(_draw_and_encode（关键）",
        "await asyncio.to_thread(\n        _draw_and_encode" in src
        or "asyncio.to_thread(_draw_and_encode" in src
        or "to_thread(\n        _draw_and_encode" in src,
    )
    check_true(
        "canvas.save 没有开 optimize=True",
        'format="PNG", optimize=True' not in src,
    )
    check_true(
        "_rounded_mask 在绘制循环里被复用（不再每张新建 mask）",
        "_rounded_mask(cover_size" in src
        and "Image.new(\"L\", (cover_size, cover_size), 0)" not in src,
    )
    check_true("有封面缓存", "_cover_cache" in src and "_COVER_CACHE_MAX" in src)
    check_true(
        "网易云小图改写有明确判据",
        '"music.126.net" in url' in src,
    )


def main() -> int:
    print("=" * 72)
    print("点歌列表图「拼图」性能回归测试")
    print("=" * 72)

    part_a_thumb_url()
    part_b_mask_cache()
    part_c_cover_cache()
    part_d_decode()
    part_e_source_guard()

    print("\n" + "=" * 72)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：")
        for f in _FAILED:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
