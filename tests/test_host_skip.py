#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官机图集：跳过「注定转存失败」的 host。

**背景**（2026-09-26 线上日志）：

官机图集要等 **4~8 秒**才发得出来。原因是对抖音签名图 CDN
（``p3-pc-sign.douyinpic.com``）做转存 —— 那个域名**本机 403、czoss 也 403**，
只有**腾讯自己的下载器**能取到。所以转存是白试：

```
3 次尝试 + 2 × 0.8 秒间隔 ≈ 2.4 秒
→ 还要再走一整套「本机下载 → 图床 → 转存」（又是几秒）
→ 最终结果仍然是「保留原始直链」
```

改成**命中名单就直接跳过**，把原始直链交给腾讯。

锁住的东西：

- 命中名单（含子域）才跳过
- **不能误伤**相似但不同的域名（``notdouyinpic.com`` / ``douyinpic.com.evil.com``）
- 跳过必须发生在 ``transfer_url`` **之前** —— 放到后面就等于没优化
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _L:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def debug(self, *a, **k): pass

    _api.logger = _L()
    _a.api = _api
    sys.modules["astrbot"] = _a
    sys.modules["astrbot.api"] = _api

from astrbot_plugin_rconsole.core.image_bed import (  # noqa: E402
    SKIP_TRANSFER_HOSTS,
    should_skip_transfer,
)

PASS = FAIL = 0

ROOT = Path(__file__).resolve().parent.parent
BED_SRC = (ROOT / "core" / "image_bed.py").read_text(encoding="utf-8")
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {msg}")
    else:
        FAIL += 1
        print(f"[FAIL] {msg}")


def test_skip_match() -> None:
    print("\n--- 该跳过的 ---")
    # 真实日志里出现过的形态
    SKIP = [
        "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c001/o84cA2Zi0SErWAK1AsA"
        "~tplv-dy-aweme-images:q75.webp?biz_tag=aweme_images",
        "https://p3-pc-sign.douyinpic.com/tos-cn-i-dy/02a1acb1fe334ed5943265352",
        "https://p6-sign.douyinpic.com/x.jpg",
        "https://p9-pc-sign.douyinpic.com:443/x.jpg",   # 带端口
        "https://douyinpic.com/x.jpg",                  # 裸域
    ]
    for u in SKIP:
        check(should_skip_transfer(u), f"跳过: {u[:78]}")

    print("\n--- 不该跳过的（防误伤）---")
    KEEP = [
        "https://iili.io/nAzEQ9t.jpg",                  # 图床
        "https://czoss.czcn.xyz/upload/mobile_1.jpg",   # 转存产物
        "https://api.elaina.cat/random/mobile",         # 随机图 API
        "https://i0.hdslb.com/bfs/bangumi/x.jpg",       # B 站封面
        "https://p1.music.126.net/x.jpg",               # 网易云封面
        # ⚠️ 相似但不同的域：必须**不**命中
        "https://notdouyinpic.com/x.jpg",
        "https://douyinpic.com.evil.com/x.jpg",
        "https://xdouyinpic.com/x.jpg",
        # 非 http / 空
        "",
        "data:image/png;base64,iVBORw0KGgo=",
        "not a url",
    ]
    for u in KEEP:
        check(not should_skip_transfer(u), f"不跳过: {u[:78]}")

    check(len(SKIP_TRANSFER_HOSTS) >= 1, "名单非空")
    check("douyinpic.com" in SKIP_TRANSFER_HOSTS, "名单里有 douyinpic.com")


def test_wiring() -> None:
    print("\n--- 接线（跳过必须发生在 transfer_url 之前）---")
    check("SKIP_TRANSFER_HOSTS" in BED_SRC, "image_bed.py 定义了 SKIP_TRANSFER_HOSTS")
    check("def should_skip_transfer" in BED_SRC, "image_bed.py 定义了 should_skip_transfer")
    check(
        "should_skip_transfer" in MAIN_SRC.split("from .core.image_bed import")[1][:200]
        or "should_skip_transfer, transfer_url" in MAIN_SRC,
        "main.py 从 image_bed 导入 should_skip_transfer",
    )

    # 定位 _host_one 函数体，比较两个调用的先后
    body = MAIN_SRC.split("async def _host_one")[1].split("async def ")[0]
    pos_skip = body.find("should_skip_transfer(url)")
    pos_transfer = body.find("await transfer_url(url")
    check(pos_skip > 0, "_host_one 里调用了 should_skip_transfer(url)")
    check(pos_transfer > 0, "_host_one 里仍会调用 transfer_url(url)")
    check(
        pos_skip < pos_transfer,
        f"跳过发生在转存**之前**（skip@{pos_skip} < transfer@{pos_transfer}）",
    )
    check(
        "签名 CDN" in body,
        "_host_one 的注释说明了为什么跳过（签名 CDN）",
    )

    # _host_images 的日志要能区分「跳过」与「真失败」
    img_body = MAIN_SRC.split("async def _host_images")[1].split("async def ")[0]
    check(
        "直接跳过转存" in img_body,
        "_host_images 的日志区分了「跳过」和「试过没成」",
    )


def main() -> int:
    test_skip_match()
    test_wiring()
    print(f"\n{'=' * 52}\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
