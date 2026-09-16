"""抖音图集「动图 / 静态图」分流逻辑的离线回归测试。

**为什么要有这个测试**：动图误判这个问题踩过两次坑——

1. 早期按「整条作品有没有动图」一刀切，导致混合图集里的静态图被丢掉。
2. 只看 SSR 路径，而 SSR 页面会把动图的 ``images[].video`` 抹掉、
   把 ``aweme_type`` 从 68 降成 2，导致动图永远被当成静态图。

第 2 点的实测对照（记录在 ``core/douyin_ssr.py`` 的模块 docstring 里）：

========================  =============  ==================  ============
通道                      aweme_type     images[].video      能否识别动图
========================  =============  ==================  ============
主接口（a-bogus）        **68**         **有完整视频轨**    ✅
SSR ``share/note``        **2**          **None（被抹掉）**  ❌
SSR ``share/slides``      无 ``_ROUTER_DATA``，拿不到数据   ❌
========================  =============  ==================  ============

跑法（需要能 import astrbot.api，或先桩掉它）::

    python tests/test_douyin_album.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# 允许直接跑：把插件根目录塞进 sys.path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 本地没有 AstrBot 运行环境时，桩掉日志器（被测逻辑不依赖它）
if "astrbot" not in sys.modules:
    _logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api = types.ModuleType("astrbot.api")
    _api.logger = _logger
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

from core.douyin_ssr import (  # noqa: E402
    album_items,
    content_type,
    extract_aweme_id,
    is_slides_url,
    static_image_candidates,
    static_image_urls,
)


def _still(n: int) -> dict:
    """一张静态图：只有 url_list，多个 CDN 候选。"""
    return {
        "url_list": [
            f"https://p3-sign.douyinpic.com/img{n}.jpeg?sign=A",
            f"https://p11-sign.douyinpic.com/img{n}.jpeg?sign=B",
        ]
    }


def _animated(n: int) -> dict:
    """一张动图：自带视频轨（play_addr_h264.uri 是判定依据）。"""
    return {
        "url_list": [f"https://p3-sign.douyinpic.com/cover{n}.jpeg?sign=C"],
        "video": {"play_addr_h264": {"uri": f"v0300fg10000anim{n}"}},
    }


_FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       期望: {want}")
        print(f"       实际: {got}")
        _FAILED.append(name)


def main() -> int:
    # ---- 1. 纯静态图集 ----
    check(
        "纯静态图集 -> 全部 still",
        [i["kind"] for i in album_items({"images": [_still(1), _still(2)]})],
        ["still", "still"],
    )

    # ---- 2. 纯动图 ----
    anim = album_items({"images": [_animated(1), _animated(2)]})
    check("纯动图 -> 全部 animated", [i["kind"] for i in anim], ["animated", "animated"])
    check(
        "每张动图的播放地址来自各自的视频轨 uri",
        [
            ("v0300fg10000anim1" in anim[0]["video_url"], anim[0]["video_uri"]),
            ("v0300fg10000anim2" in anim[1]["video_url"], anim[1]["video_uri"]),
        ],
        [(True, "v0300fg10000anim1"), (True, "v0300fg10000anim2")],
    )

    # ---- 3. 混排（核心场景：顺序必须保持） ----
    mixed = {"images": [_still(1), _animated(2), _still(3), _animated(4)]}
    check(
        "混排图集 -> 顺序保持静-动-静-动",
        [i["kind"] for i in album_items(mixed)],
        ["still", "animated", "still", "animated"],
    )

    # ---- 4. 动图不能混进静态图列表 ----
    check(
        "static_image_urls 自动跳过动图",
        len(static_image_urls(mixed)),
        2,
    )
    check(
        "static_image_candidates 自动跳过动图",
        len(static_image_candidates(mixed)),
        2,
    )

    # ---- 5. 类型判定 ----
    check(
        "aweme_type=68（动图集）判为 image",
        content_type({"aweme_type": 68, "images": [_animated(1)]}),
        "image",
    )
    check(
        "aweme_type=0 判为 video",
        content_type({"aweme_type": 0, "video": {"play_addr": {"uri": "x"}}}),
        "video",
    )

    # ---- 6. slides 链接识别（必须走主接口） ----
    check(
        "share/slides 链接能被识别",
        is_slides_url("https://www.iesdouyin.com/share/slides/7676017468716436809/"),
        True,
    )
    check(
        "slides 链接能抽出 aweme_id",
        extract_aweme_id("https://www.iesdouyin.com/share/slides/7676017468716436809/"),
        "7676017468716436809",
    )
    check(
        "普通 note 链接不算 slides",
        is_slides_url("https://www.iesdouyin.com/share/note/123/"),
        False,
    )

    print("\n" + "=" * 52)
    if _FAILED:
        print(f"{len(_FAILED)} 项失败：{', '.join(_FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
