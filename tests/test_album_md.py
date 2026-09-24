"""抖音图集 → 官机「一条 markdown 内嵌多图」回归测试。

**为什么要它**：官机上「图集发不出来」有两个独立原因，各自都能悄悄退化 ——

1. **图在本机 403**（去 ~tplv 后缀 / 换 p3→p9→p6 节点 / 加 Referer / 带 Cookie
   全部无效）。markdown 内嵌图是**腾讯服务器**去下载的，正好绕开这条路。
2. **尺寸必须带**。`![图1 #300px #400px](url)` 不带尺寸时**电脑 QQ 照常显示、
   手机 QQ 只渲染 `[alt]`**（v1.6.7 实测踩过）。尺寸靠抖音 `images[i]` 顶层
   自带的 `width`/`height`，所以这条链路断了就会退化成「一堆 [alt]」。

跑法::

    python tests/test_album_md.py
"""

from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from astrbot_plugin_rconsole.core.douyin_ssr import (  # noqa: E402
    album_items,
    image_size,
)

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


URL_A = "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/aaa~tplv-dy-aweme-images:q75.webp?sig=1"
URL_B = "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/bbb~tplv-dy-aweme-images:q75.webp?sig=1"


# ==========================================================================
# A) image_size —— 尺寸来源与兜底
# ==========================================================================
def part_a_size() -> None:
    print("\n[A] image_size")
    # 实测：抖音在 images[i] **顶层**就给 width/height
    check("顶层 width/height", image_size({"width": 1612, "height": 1538}), (1612, 1538))
    check(
        "顶层没有时兜底 origin_url",
        image_size({"origin_url": {"width": 800, "height": 600}}),
        (800, 600),
    )
    check(
        "顶层优先于 origin_url",
        image_size(
            {"width": 10, "height": 20, "origin_url": {"width": 999, "height": 999}}
        ),
        (10, 20),
    )
    # ---- 取不到就该放弃这一段，而不是返回个瞎猜的值 ----
    check("两层都没有 -> None", image_size({}), None)
    check("只有宽度 -> None", image_size({"width": 100}), None)
    check("0 不算（宽高必须为正）", image_size({"width": 0, "height": 100}), None)
    check("负数不算", image_size({"width": -3, "height": 100}), None)
    check(
        "字符串数字能转",
        image_size({"width": "1080", "height": "1920"}),
        (1080, 1920),
    )
    check(
        "非数字字符串 -> None",
        image_size({"width": "abc", "height": "1920"}),
        None,
    )
    check("origin_url 不是 dict 时不炸", image_size({"origin_url": "http://x"}), None)


# ==========================================================================
# B) album_items 把尺寸带出来
# ==========================================================================
def _aweme_with(images: list[dict]) -> dict:
    return {"images": images}


def part_b_items() -> None:
    print("\n[B] album_items 带尺寸")
    aweme = _aweme_with([
        {"url_list": [URL_A], "width": 1612, "height": 1538},
        {"url_list": [URL_B], "width": 900, "height": 1600},
    ])
    items = album_items(aweme)
    check("两项都识别为 still", [it["kind"] for it in items], ["still", "still"])
    check("第 1 项尺寸", items[0].get("size"), (1612, 1538))
    check("第 2 项尺寸", items[1].get("size"), (900, 1600))
    check("顺序与作品一致（index）", [it["index"] for it in items], [0, 1])

    # 动图项：没有图片，size 应该是 None（不该凭空造一个）
    anim = _aweme_with([
        {
            "url_list": [URL_A],
            "width": 100, "height": 200,
            "video": {"play_addr": {"uri": "v0200fg10000abc", "url_list": []}},
        }
    ])
    anim_items = album_items(anim)
    check("带视频轨的判为 animated", anim_items[0]["kind"], "animated")
    check_true("animated 项没有 size 字段", "size" not in anim_items[0])


# ==========================================================================
# C) main.py 组装逻辑（源码不变量）
# ==========================================================================
def part_c_main() -> None:
    print("\n[C] main.py 组装逻辑")
    src = (_ROOT / "main.py").read_text(encoding="utf-8")

    check_true("有 _gallery_md_text（通用拼装入口）", "async def _gallery_md_text" in src)
    check_true(
        "抖音图集走同一个入口",
        "await self._gallery_md_text(event, result, list(result.images))" in src,
    )
    check_true(
        "官机分支要求「纯静态图集」",
        'self._caps(event).markdown and all(k == "still" for k in kinds)' in src,
        "markdown 里塞不进视频，含动图必须走原路径",
    )
    check_true("拼好之后开了 markdown", "chain.use_markdown(True)" in src)
    check_true(
        "缺尺寸时整体放弃（不退回逐条）",
        "if not size:\n                return None" in src,
        "宁可逐条发，也别发一条手机上全是 [alt] 的消息",
    )
    check_true("理由写进了注释（403 绕开）", "绕开了本机对抖音图的 403" in src)
    check_true(
        "用了 _md_image 拼内嵌图（尺寸走同一个入口）",
        "self._md_image(url, size, alt=f\"图{i}\"" in src,
    )

    # 尺寸要真的从 extra 里读
    check_true(
        "从 extra['image_sizes'] 读尺寸",
        'result.extra.get("image_sizes")' in src,
    )

    # ---- 换行：图片之间必须**空行分隔**（v1.6.11 修的真 bug）----
    #
    # 官方文档「换多行」明确：单换行**不产生换行效果**，要用空行。三行
    # `![…]` 紧贴时会被当成同一段文本 —— 手机 QQ 上只渲染第一张
    # （实测：三张图的图集只出来一张）。
    body = src.split("async def _gallery_md_text")[1].split("async def _send_album")[0]
    check_true(
        "图集 MD：图片按**一行 2~3 张**排（用户实测这个最合适）",
        "per_row" in body and '" ".join(blocks[' in body,
        "一行内要用空格分隔才横排",
    )
    check_true(
        "图集 MD：**行与行之间**用空行分隔（官方：单换行不换行）",
        '\\n\\n".join(' in body,
    )
    check_true(
        "图集 MD：不再用单换行直接拼接图片块",
        "lines += blocks" not in body,
    )

    # 自检命令里的多图对照也必须体现这个差别（否则自检本身就是错的）
    check_true(
        "#RMD图：4/4 用空行分隔（正确写法）",
        '\\n\\n".join' in src,
        "自检要能对比出「单换行 vs 空行」的差别",
    )


# ==========================================================================
# D) platforms/douyin.py 传递链
# ==========================================================================
def part_d_pass() -> None:
    print("\n[D] 尺寸传递链")
    src = (_ROOT / "platforms" / "douyin.py").read_text(encoding="utf-8")
    check_true("收集 image_sizes", "image_sizes.append(it.get(\"size\"))" in src)
    check_true("写进 extra", 'extra["image_sizes"] = image_sizes' in src)

    ssr = (_ROOT / "core" / "douyin_ssr.py").read_text(encoding="utf-8")
    check_true("album_items 写了 size", '"size": image_size(image),' in ssr)
    check_true("有 image_size 函数", "def image_size(image: dict" in ssr)


def main() -> int:
    part_a_size()
    part_b_items()
    part_c_main()
    part_d_pass()
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
