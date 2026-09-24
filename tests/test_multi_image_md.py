"""官机「多图 MD」回归测试（v1.6.15）。

官机没有合并转发（``Comp.Nodes`` 适配器不认），所以 N 张图只能：
- 逐条发 → **刷屏**；或
- ``plugin.max_images`` 限制「只发前 N 张」→ 用户实测过「三张图只出来一张」。

解法是拼成**一条 markdown 内嵌多图**。两个关键点：

1. **所有图片先转存到国内 OSS**。官机 markdown 的图是**腾讯服务器下载转存**的
   （官方文档原话），而解析出来的原始直链常常取不到 —— 抖音
   ``p3-sign.douyinpic.com`` 要 ``Referer``（本机实测恒定 403）、各家 CDN 也未必
   对腾讯的下载器友好。统一过一遍 ``transfer_url``（czoss，广州电信）之后，
   交给腾讯的是「国内 + 内容固定 + https」的地址。
2. **排版一行 2~3 张**、且多图用更小的外显尺寸。用户实测对比后确认：
   每行一张最刷屏；一行两张 / 三张最合适，三张再等比缩一档。

跑法::

    python tests/test_multi_image_md.py
"""

from __future__ import annotations

import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = (_ROOT / "main.py").read_text(encoding="utf-8")

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


def _body() -> str:
    """``_gallery_md_text`` 的方法体源码。"""
    return MAIN.split("async def _gallery_md_text")[1].split("async def _send_album")[0]


# ==========================================================================
# A) 转存：所有图片先落到国内 OSS
# ==========================================================================

def part_a_host() -> None:
    print("\n[A] 图片先转存国内 OSS")
    body = _body()

    check_true("有 _host_images", "async def _host_images" in MAIN)
    check_true(
        "**_gallery_md_text 里调了它**（转存是拼 MD 的前置步骤）",
        "await self._host_images(urls)" in body,
    )
    check_true(
        "**转存在拼图之前**（顺序不能反）",
        body.index("await self._host_images(urls)") < body.index("self._md_image("),
    )
    check_true(
        "转存不全就整体放弃（退回逐条发送，不发半残的 MD）",
        "if not hosted:" in body and "return None" in body,
    )
    host = MAIN.split("async def _host_images")[1].split("async def _gallery_md_text")[0]
    check_true("走 transfer_url（czoss 国内节点）", "transfer_url(u, key=key)" in host)
    check_true(
        "**并发**转存（9 张串行要 40 秒）",
        "asyncio.gather(" in host,
        "串行会明显卡住用户",
    )
    check_true("任意一张失败返回 None", "return None" in host)
    check_true("用配置里的 key", "self._czoss_key()" in host)


# ==========================================================================
# B) 排版：一行 2~3 张 + 多图更小
# ==========================================================================

def part_b_layout() -> None:
    print("\n[B] 排版：一行 2~3 张")
    body = _body()

    check_true("有 _md_multi_image_width（多图专用宽度）",
               "def _md_multi_image_width" in MAIN)
    check_true(
        "**单图宽度方法没被顶掉**（改这个文件时踩过：替换区间把整个方法吃掉了）",
        "def _md_image_width(" in MAIN and "def _md_multi_image_width" in MAIN,
    )
    check_true("默认 170px", '"md_multi_image_width", 170' in MAIN)
    check_true("夹在 80~400", "max(80, min(400, w))" in MAIN)

    check_true("一行 3 张的分支", "per_row = 3" in body)
    check_true("一行 2 张的分支", "per_row = 2" in body)
    check_true(
        "一行三张再等比缩一档",
        "base * 2 // 3" in body,
        "三张并排太宽会挤",
    )
    check_true(
        "单图仍用单图宽度",
        "self._md_image_width(event)" in body,
    )
    check_true(
        "**一行内用空格分隔**（才能横排）",
        '" ".join(blocks[' in body,
    )
    check_true(
        "**行间用空行**（官方：单换行不换行，手机只渲染第一张）",
        '"\\n\\n".join(' in body,
    )
    check_true(
        "尺寸小只是外显，URL 不变（点开仍是原图）",
        "max_width=max_width" in body,
    )


# ==========================================================================
# C) schema
# ==========================================================================

def part_c_schema() -> None:
    print("\n[C] schema 配置项")
    schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    items = schema["plugin"]["items"]

    check_true("有 mdMultiImageWidth", "mdMultiImageWidth" in items)
    check("默认 170", items.get("mdMultiImageWidth", {}).get("default"), 170)
    check("mdImageWidth 仍是 300（单图）",
          items.get("mdImageWidth", {}).get("default"), 300)
    check_true(
        "main.py 按协议端读 md_multi_image_width（schema 里没有的键会被裁掉）",
        '"md_multi_image_width"' in MAIN,
    )


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 官机多图 MD 回归测试")
    print("=" * 70)
    part_a_host()
    part_b_layout()
    part_c_schema()

    print()
    print("=" * 70)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for item in FAILED:
            print("   -", item)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
