"""菜单图床（``core/image_bed.py``）回归测试。

**为什么需要"图床"这一步**（三个约束逼出来的）：

1. QQ markdown 内嵌图要求公网 URL —— 而 ``Comp.Image``（media 段）和
   markdown **互斥**（适配器会 ``payload.pop("markdown")``），所以菜单图
   只能走 MD 内嵌；
2. **URL 必须每次不同** —— 同一个 URL 会被客户端缓存，菜单图就永远是同一张；
3. **markdown 内嵌图必须带尺寸** —— 而随机图 API 每次给的是**另一张**图，
   尺寸只能插件本地读出来再写进 MD。

选 freeimage.host 是**实测**结果（2026-09-23）：

    Telegraph      ❌ 上传返回 "Unknown error"
    Catbox         ❌ Invalid uploader
    0x0.st         ❌ 官方关闭上传（"AI botnet spam"）
    sm.ms 匿名     ❌ 空响应（需 token）
    freeimage.host ✅ 免注册（有公开测试 key），返回 iili.io 直链
                      —— 实测**国内可直读**（腾讯取图没问题）

跑法::

    python tests/test_image_bed.py
"""

from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_DIR = _ROOT / "core"
MAIN = (_ROOT / "main.py").read_text(encoding="utf-8")
BED = (SRC_DIR / "image_bed.py").read_text(encoding="utf-8")

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


# ==========================================================================
# A) 图床模块契约
# ==========================================================================

def part_a_module() -> None:
    print("\n[A] core/image_bed.py 契约")
    check_true("模块存在", (SRC_DIR / "image_bed.py").is_file())
    check_true("有 upload_image", "async def upload_image" in BED)
    check_true(
        "用的是 freeimage.host 的上传接口",
        "https://freeimage.host/api/1/upload" in BED,
    )
    check_true(
        "带公开测试 key 兜底（配置为空时也能用）",
        "6d207e02198a847aa98d0a2a901485a5" in BED,
    )
    check_true(
        "**失败返回空串、绝不抛异常**（图床挂了不能拖垮菜单）",
        "return \"\"" in BED and "except Exception as exc:" in BED,
        "调用方靠空串判断降级",
    )
    check_true(
        "返回的 url 会校验 http 前缀（不合法就当失败）",
        'if not url.startswith("http"):' in BED,
    )
    check_true(
        "走共用连接池（get_session），不是每次新建 session",
        "from .http import get_session" in BED,
    )
    check_true(
        "docstring 里记了实测过的图床对比（免得以后重复试）",
        "0x0.st" in BED and "Invalid uploader" in BED,
    )


# ==========================================================================
# B) 菜单接线
# ==========================================================================

def part_b_menu() -> None:
    print("\n[B] 菜单接线")
    check_true("main.py 引入了 upload_image", "from .core.image_bed import upload_image" in MAIN)
    check_true("有 _menu_image_md", "async def _menu_image_md" in MAIN)

    read_size = MAIN.find("PILImage.open(BytesIO(body))")
    upload = MAIN.find("upload_image(body")
    check_true(
        "**先本地读尺寸、再上传**（顺序不能反）",
        read_size != -1 and upload != -1 and read_size < upload,
        "随机图每次不一样，尺寸只能本地读",
    )

    check_true(
        "菜单 MD 里只有图（用户要求：不要文字说明）",
        'self._md_image(\n            url, size, alt="菜单"' in MAIN,
    )
    check_true(
        "降级链：随机图 → 纯文字 MD 菜单 → 本地渲染图",
        "_menu_markdown(bot_name), rows" in MAIN,
    )
    check_true(
        "随机图 API 可配置，默认竖屏档",
        "def _menu_image_api" in MAIN
        and "https://api.elaina.cat/random/mobile" in MAIN,
    )
    check_true("图床 key 可配置", "def _image_bed_key" in MAIN)
    check_true(
        "注释里记了「为什么不能用 API 原名当 MD 图 URL」",
        "缓存" in MAIN and "尺寸对不上就会变形" in MAIN,
    )


# ==========================================================================
# C) schema
# ==========================================================================

def part_c_schema() -> None:
    print("\n[C] schema 配置项")
    import json

    schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema["plugin"]["items"]

    check_true("有 menuImageApi", "menuImageApi" in items)
    check_true("有 imageBedKey", "imageBedKey" in items)
    check(
        "menuImageApi 默认值 = 竖屏档",
        items.get("menuImageApi", {}).get("default"),
        "https://api.elaina.cat/random/mobile",
    )
    check("imageBedKey 默认留空（用公开 key）", items.get("imageBedKey", {}).get("default"), "")

    # 代码里读的键必须在 schema 里（AstrBot 会按 schema 裁剪配置）
    for key in ("plugin.menuImageApi", "plugin.imageBedKey"):
        check_true(f"main.py 读 {key}（schema 里有才不会丢）", key in MAIN)


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 菜单图床回归测试")
    print("=" * 70)
    part_a_module()
    part_b_menu()
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
