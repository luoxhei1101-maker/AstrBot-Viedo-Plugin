"""菜单配图（``core/image_bed.py``）回归测试。

**菜单图为什么这么绕**（三个约束逼出来的）：

1. QQ markdown 内嵌图要求公网 URL —— 而 ``Comp.Image``（media 段）和
   markdown **互斥**（适配器会 ``payload.pop("markdown")``），所以菜单图
   只能走 MD 内嵌；
2. **URL 必须每次不同** —— 同一个 URL 会被客户端缓存，菜单图就永远是同一张；
3. **markdown 内嵌图必须带尺寸** —— 而随机图 API 每次给的是**另一张**图，
   尺寸只能先落到一个「内容固定」的 URL 上才量得准。

**关键事实**：官机 markdown 的图是**腾讯服务器下载转存**的（官方文档原话），
所以放图的域名必须**国内能取到**。实测（2026-09-23）：

    freeimage.host → iili.io（Cloudflare）  ❌ 广州本地读得到，
                                               但放进官机 MD 后客户端报「图片加载失败」
    api.czcn.xyz → czoss.czcn.xyz（国内）    ✅ 广州电信 113.96.129.x，Server: ESA；
                                               内容固定；能吃随机图 API 的地址

所以主路径是**国内转存**，图床（``upload_image``）退为兜底。

跑法::

    python tests/test_image_bed.py
"""

from __future__ import annotations

import json
import pathlib

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
# A) 国内转存（主路径）
# ==========================================================================

def part_a_transfer() -> None:
    print("\n[A] transfer_url（国内转存，主路径）")
    check_true("有 transfer_url", "async def transfer_url" in BED)
    check_true(
        "用的是 api.czcn.xyz 的转存接口",
        "https://api.czcn.xyz/api/czoss" in BED,
    )
    check_true(
        "**http:// 会改写成 https://**",
        'if url.startswith("http://"):' in BED.replace("  ", ""),
        "markdown 内嵌图实测必须 https，而它默认回 http",
    )
    check_true(
        "取 data.direct_url",
        'data.get("direct_url")' in BED,
    )
    check_true(
        "校验 status == success",
        'payload.get("status") != "success"' in BED,
    )
    check_true(
        "**非图片 mime 直接丢弃**（防止把错误页当图）",
        "detected_mime" in BED and 'mime.startswith("image/")' in BED,
    )
    check_true(
        "失败返回空串、绝不抛异常",
        'return ""' in BED and "except Exception as exc:" in BED,
    )
    check_true(
        "走共用连接池（get_session），不是每次新建 session",
        "from .http import get_session" in BED,
    )
    check_true(
        "key 为空时不拼 key 参数（实测不带也能用）",
        'if (key or "").strip():' in BED,
    )
    check_true(
        "docstring 记了实测结论（国内节点 + 内容固定 + 为什么 iili.io 不行）",
        "113.96.129" in BED and "ESA" in BED and "图片加载失败" in BED,
    )


# ==========================================================================
# B) 图床（兜底路径，不能被删）
# ==========================================================================

def part_b_bed() -> None:
    print("\n[B] upload_image（图床，降级兜底）")
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
        "返回的 url 会校验 http 前缀（不合法就当失败）",
        'if not url.startswith("http"):' in BED,
    )
    check_true(
        "docstring 里记了实测过的图床对比（免得以后重复试）",
        "0x0.st" in BED and "Invalid uploader" in BED,
    )


# ==========================================================================
# C) 菜单接线
# ==========================================================================

def part_c_menu() -> None:
    print("\n[C] 菜单接线")
    check_true(
        "main.py 引入了 transfer_url 和 upload_image",
        "from .core.image_bed import transfer_url, upload_image" in MAIN,
    )
    check_true(
        "**那条 import 必须顶格**（缩进错了整份文件加载不了）",
        "\nfrom .core.image_bed import transfer_url, upload_image\n" in MAIN,
        "2026-09-23 踩过：脚本给顶格的 import 多加了 4 空格 -> IndentationError",
    )
    check_true("有 _menu_image_md", "async def _menu_image_md" in MAIN)
    check_true(
        "有降级方法 _menu_image_md_by_bed",
        "async def _menu_image_md_by_bed" in MAIN,
    )

    md = MAIN.find("async def _menu_image_md(")
    bed = MAIN.find("async def _menu_image_md_by_bed")
    call_transfer = MAIN.find("await transfer_url(api", md)
    call_bed = MAIN.find("await self._menu_image_md_by_bed(event, api)", md)
    check_true(
        "**转存优先、图床兜底**（顺序不能反）",
        md != -1 and call_transfer != -1 and call_bed != -1 and call_transfer < call_bed,
        "图床在 CF 上，腾讯侧取不到 —— 那是「图片加载失败」的根因",
    )
    check_true(
        "降级路径里仍是「先读尺寸、再上图床」",
        bed != -1
        and MAIN.find("PILImage.open(BytesIO(body))", bed)
        < MAIN.find("upload_image(body", bed),
    )
    check_true(
        "菜单 MD 里只有图（用户要求：不要文字说明）",
        'alt="菜单"' in MAIN,
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
    check_true("转存 key 可配置", "def _czoss_key" in MAIN)
    check_true("图床 key 仍可配置", "def _image_bed_key" in MAIN)


# ==========================================================================
# D) schema
# ==========================================================================

def part_d_schema() -> None:
    print("\n[D] schema 配置项")
    schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    items = schema["plugin"]["items"]

    check_true("有 menuImageApi", "menuImageApi" in items)
    check_true("有 imageBedKey", "imageBedKey" in items)
    check_true("有 czossKey", "czossKey" in items)
    check(
        "menuImageApi 默认值 = 竖屏档",
        items.get("menuImageApi", {}).get("default"),
        "https://api.elaina.cat/random/mobile",
    )
    check("imageBedKey 默认留空（用公开 key）", items.get("imageBedKey", {}).get("default"), "")
    check(
        "czossKey 默认留空（实测不带也能用，且不把 key 写进公开仓库）",
        items.get("czossKey", {}).get("default"),
        "",
    )

    # 代码里读的键必须在 schema 里（AstrBot 会按 schema 裁剪配置）
    for key in ("plugin.menuImageApi", "plugin.imageBedKey", "plugin.czossKey"):
        check_true(f"main.py 读 {key}（schema 里有才不会丢）", key in MAIN)


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 菜单配图回归测试")
    print("=" * 70)
    part_a_transfer()
    part_b_bed()
    part_c_menu()
    part_d_schema()

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
