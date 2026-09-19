"""抖音图集「原图优先」候选排序的离线回归测试（v1.6.2）。

**为什么要有这个测试**：用户反馈「每次解析最好看的那张没了 / 下载失败」。
实测（2026-09-20，两条真实图集、7 张图、每张 3 轮探测）拿到了硬数据：

======================  =============  ==========  ==========================
候选位置                节点           后缀        实测结果
======================  =============  ==========  ==========================
[0]                     p3-pc-sign     .webp       **可能 403**（每张图固定）
[1]                     p9-pc-sign     .webp       基本 200，但是**压缩预览**
[2]                     p3-pc-sign     .jpeg       基本 200，**是原图**
======================  =============  ==========  ==========================

两个必须锁住的不变量：

1. **`.jpeg/.png` 必须排在 `.webp` 前面。** ``tplv-dy-aweme-images:q75`` 是
   质量 75 的压缩模板，实测同一张图 `.webp` 107464 字节 vs `.jpeg` 269965
   字节——差一倍以上。按 ``url_list`` 原顺序「试到第一个成功就用」会拿到
   模糊的缩略图，这就是「最好看的那张变糊了」的根因。
2. **403 是每张图固定的，不是随机的。** 所以「多试几次会好」是错判；
   必须保证候选顺序正确 + 逐个回退，才能在候选少的图上也不丢图。

跑法（需要能 import astrbot.api，或先桩掉它）::

    python tests/test_douyin_album_candidates.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

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
    _image_kind_rank,
    album_items,
    rank_image_candidates,
    static_image_candidates,
)

# ---- 真实抓到的候选 URL（2026-09-20，作品 7687241865309237105）----
# 顺序就是抖音给的 url_list 原顺序：[0] p3 webp → [1] p9 webp → [2] p3 jpeg
REAL_WEBP_P3 = (
    "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/o81An1uSTC1E0Aie"
    "~tplv-dy-aweme-images:q75.webp?lk3s=138a59ce&x-expires=1792450800"
    "&x-signature=abc%3D&from=327834062&biz_tag=aweme_images"
)
REAL_WEBP_P9 = (
    "https://p9-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/o81An1uSTC1E0Aie"
    "~tplv-dy-aweme-images:q75.webp?lk3s=138a59ce&x-expires=1792450800"
    "&x-signature=abc%3D&from=327834062&biz_tag=aweme_images"
)
REAL_JPEG_P3 = (
    "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/o81An1uSTC1E0Aie"
    "~tplv-dy-aweme-images:q75.jpeg?lk3s=138a59ce&x-expires=1792450800"
    "&x-signature=abc%3D&from=327834062&biz_tag=aweme_images"
)

_FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       期望: {want}")
        print(f"       实际: {got}")
        _FAILED.append(name)


def check_true(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        if detail:
            print(f"       {detail}")
        _FAILED.append(name)


def main() -> int:  # noqa: C901 - 测试函数，线性断言更易读
    # ---- 1. 格式优先级 ----
    check("jpeg 排最前（rank 0）", _image_kind_rank(REAL_JPEG_P3), 0)
    check("webp 排中间（rank 1）", _image_kind_rank(REAL_WEBP_P3), 1)
    check(
        "未知格式排最后（rank 2）",
        _image_kind_rank("https://x.com/a.bin"),
        2,
    )
    check(
        "png 也算原图（rank 0）",
        _image_kind_rank("https://p3-sign.douyinpic.com/a~tplv.avif.png?x=1"),
        0,
    )
    check(
        "只看路径不看 query（.webp 在 query 里不算）",
        _image_kind_rank("https://p3-sign.douyinpic.com/a.jpeg?fmt=.webp"),
        0,
    )

    # ---- 2. 核心：真实候选重排后 jpeg 必须排第一 ----
    #   这是「最好看的那张没了」的直接修复点
    original = [REAL_WEBP_P3, REAL_WEBP_P9, REAL_JPEG_P3]
    ranked = rank_image_candidates(original)
    check(
        "真实 url_list 重排后 jpeg 排第一（不再拿到 webp 缩略图）",
        ranked[0],
        REAL_JPEG_P3,
    )
    check(
        "两个 webp 候选仍保留在后面（换节点绕 403 的路不能丢）",
        len(ranked),
        3,
    )
    check_true(
        "重排后 size 不变（不丢候选）",
        len(ranked) == len(original) and set(ranked) == set(original),
    )

    # ---- 3. 稳定排序：同级内保持抖音原顺序 ----
    ranked2 = rank_image_candidates([REAL_WEBP_P3, REAL_WEBP_P9, REAL_JPEG_P3])
    check(
        "同级（两个 webp）内保持原相对顺序",
        [u for u in ranked2 if u.split("?")[0].endswith(".webp")],
        [REAL_WEBP_P3, REAL_WEBP_P9],
    )

    # ---- 4. jpeg 在前 + 原顺序也含 jpeg 时不重复 ----
    check(
        "已排好序的输入幂等",
        rank_image_candidates([REAL_JPEG_P3, REAL_WEBP_P3]),
        [REAL_JPEG_P3, REAL_WEBP_P3],
    )

    # ---- 5. 端到端：album_items 产出的候选已排序 ----
    aweme = {
        "images": [
            {"url_list": [REAL_WEBP_P3, REAL_WEBP_P9, REAL_JPEG_P3]},
            {"url_list": [REAL_WEBP_P3, REAL_JPEG_P3]},
        ]
    }
    items = album_items(aweme)
    check("album_items 产出 2 项都是 still", [i["kind"] for i in items], ["still", "still"])
    check_true(
        "album_items 的 image_url 是 jpeg 原图（不是 webp）",
        items[0]["image_url"] == REAL_JPEG_P3,
        f"实际 {items[0]['image_url'][:70]}",
    )
    check_true(
        "第二张图的候选也排好了（jpeg 在前）",
        items[1]["image_candidates"][0] == REAL_JPEG_P3,
    )
    check(
        "static_image_candidates 同样继承排序",
        static_image_candidates(aweme)[0][0],
        REAL_JPEG_P3,
    )

    # ---- 6. 混合后缀的无序输入：jpeg 全排到 webp 前面 ----
    a, b, c, d = (
        "https://x.com/1.webp",
        "https://x.com/2.jpeg",
        "https://x.com/3.png",
        "https://x.com/4.webp",
    )
    ranked3 = rank_image_candidates([a, b, c, d])
    check(
        "多格式混合：原图（jpeg/png）排到所有 webp 前面",
        ranked3[:2],
        [b, c],
    )

    # ---- 7. 动图项必须有 video_candidates（v1.6.2 新增，供发送端回退） ----
    anim = album_items(
        {"images": [{"url_list": [], "video": {"play_addr_h264": {
            "uri": "v0300fg10000anim",
            "url_list": ["https://v3-web.douyinvod.com/a.mp4", "https://v6-web.douyinvod.com/b.mp4"],
        }}}]}
    )
    check("动图项 kind=animated", anim[0]["kind"], "animated")
    check_true(
        "动图项带 video_candidates（首个候选是抖音给的直链）",
        anim[0]["video_candidates"][0] == "https://v3-web.douyinvod.com/a.mp4",
        f"实际 {anim[0].get('video_candidates')}",
    )
    check_true(
        "动图候选里补了 snssdk 模板兜底",
        any("aweme.snssdk.com" in u for u in anim[0]["video_candidates"]),
    )
    check_true(
        "动图候选无重复",
        len(anim[0]["video_candidates"]) == len(set(anim[0]["video_candidates"])),
    )
    check(
        "静态图项的 video_candidates 为空（不污染）",
        album_items({"images": [{"url_list": [REAL_JPEG_P3]}]})[0].get("video_candidates", []),
        [],
    )

    print("\n" + "=" * 52)
    if _FAILED:
        print(f"{len(_FAILED)} 项失败：{', '.join(_FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
