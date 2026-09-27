#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地图 → 公网直链（``core/local_pub.py`` + main.py 的接线）。

**背景**（2026-09-27 实测）：

官机发**本地生成的图**时，AstrBot 适配器只有 base64 一条路
（``upload_group_and_c2c_image`` 的 payload 里**没有 url 字段**），
对「境外机器 → 腾讯境内 API」这条链路，把整个文件传上去就是最慢的一环：

    base64 ： 97KB  → 7.39s / 14.88s
    url    ：784KB  → 3.47s     ← 体积大 8 倍反而快一倍多

所以把文件写进一个静态目录、由用户自己的 HTTP 服务暴露成 URL，
让腾讯**自己去下载**。

锁住的东西：

- base 留空 = 功能关闭（必须能降级，默认不能偷偷开启）
- 文件名按内容哈希 → 同图不重复落盘
- 单文件有上限、后缀有白名单、写盘用原子替换
- 5 个本地图调用点都要接上（漏一个就是「有的图快、有的图慢」）
- ``_upload_qq_media`` 的 base64 路**必须保留**（降级兜底）
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
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

from astrbot_plugin_rconsole.core import local_pub  # noqa: E402

PASS = FAIL = 0

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
PUB_SRC = (ROOT / "core" / "local_pub.py").read_text(encoding="utf-8")
SCHEMA = (ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig")


def check(name: str, got, expect=True, note: str = "") -> None:
    global PASS, FAIL
    ok = got == expect
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  期望={expect!r} 实际={got!r} {note}")


def check_true(name: str, got, note: str = "") -> None:
    check(name, bool(got), True, note)


# ======================================================================
# ① 纯函数
# ======================================================================
def part_a_helpers() -> None:
    print("\n--- ① base 归一化 / 后缀白名单 / 文件名 ---")
    nb = local_pub.normalize_base
    check("去掉结尾斜杠", nb("http://1.2.3.4:18900/"), "http://1.2.3.4:18900")
    check("多个斜杠也只留一个", nb("http://1.2.3.4:18900///"), "http://1.2.3.4:18900")
    check("去空白", nb("  http://a.com  "), "http://a.com")
    check("空串还是空串", nb(""), "")
    check("None 也安全", nb(None), "")

    ss = local_pub.safe_suffix
    check(".png 保留", ss(".png"), ".png")
    check("PNG 转小写", ss(".PNG"), ".png")
    check("不带点也认", ss("jpg"), ".jpg")
    check("未知后缀 → .bin", ss(".exe"), ".bin")
    check(".py 不在白名单 → .bin", ss(".py"), ".bin")
    check("空 → .bin", ss(""), ".bin")

    cn = local_pub.content_name
    a = cn(b"hello", ".png")
    b = cn(b"hello", ".png")
    c = cn(b"hellp", ".png")
    check("同内容同名（天然去重）", a == b, True)
    check("不同内容不同名", a != c, True)
    check("后缀接上了", a.endswith(".png"), True)
    check("名字长度合理（24 hex + 后缀）", len(a), 28)

    check("默认目录就是镜像数据目录下", local_pub.DEFAULT_DIR, "/AstrBot/data/rconsole_pub")
    # 比 Path 对象而不是字符串 —— Windows 上 Path("/x/y") 会写成 \x\y
    check("publish_dir 空走默认", local_pub.publish_dir(""), Path(local_pub.DEFAULT_DIR))
    check("publish_dir 有值就走值", local_pub.publish_dir("/x/y"), Path("/x/y"))


# ======================================================================
# ② publish / cleanup（真写盘）
# ======================================================================
def part_b_publish() -> None:
    print("\n--- ② publish（真写盘） ---")
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_pub_cases(Path(td)))


async def _pub_cases(td: Path) -> None:
    base = "http://154.201.73.129:18900"

    # base 为空 -> 功能关闭
    r = await local_pub.publish(b"x", base_url="", directory=str(td))
    check("base 留空 → None（功能关闭）", r, None)

    # 空数据
    r = await local_pub.publish(b"", base_url=base, directory=str(td))
    check("空数据 → None", r, None)

    # 正常
    png = b"\x89PNG\r\n\x1a\n" + b"A" * 500
    url = await local_pub.publish(png, base_url=base, directory=str(td), ttl=0)
    check_true("返回了 URL", url, str(url))
    check("URL 前缀正确", url.startswith(base + "/"), True)
    check("URL 以 .png 结尾", url.endswith(".png"), True)
    name = url.rsplit("/", 1)[1]
    fp = td / name
    check_true("文件真的落盘了", fp.exists())
    check("大小一致", fp.stat().st_size, len(png))
    check("内容一致", fp.read_bytes(), png)
    check("没有残留 .part", list(td.glob("*.part")), [])

    # 同内容再发一次 -> 不重复落盘
    url2 = await local_pub.publish(png, base_url=base, directory=str(td), ttl=0)
    check("同内容 URL 相同", url2, url)
    check("目录里只有 1 个文件", len(list(td.iterdir())), 1)

    # 不同内容 -> 第 2 个文件
    png2 = png + b"B"
    url3 = await local_pub.publish(png2, base_url=base, directory=str(td), ttl=0)
    check_true("不同内容给出不同 URL", url3 and url3 != url)
    check("目录里现在 2 个文件", len(list(td.iterdir())), 2)

    # 超大文件拒绝
    huge = b"x" * (local_pub.MAX_BYTES + 1)
    r = await local_pub.publish(huge, base_url=base, directory=str(td))
    check("超上限 → None", r, None)

    # cleanup：把旧文件的 mtime 拨回去，应当被清掉；新文件留着
    old = td / "old.bin"
    old.write_bytes(b"old")
    past = time.time() - 9999
    import os
    os.utime(old, (past, past))
    removed = local_pub.cleanup(str(td), ttl=600)
    check("cleanup 删掉 1 个旧文件", removed, 1)
    check("旧文件没了", old.exists(), False)
    check_true("新文件还在", (td / name).exists())

    # ttl<=0 不清理
    r = await local_pub.publish(b"zzz", base_url=base, directory=str(td), ttl=0)
    check_true("ttl=0 也能正常发布", r)

    # 目录不存在也不该炸
    check("cleanup 对不存在目录返回 0", local_pub.cleanup(str(td / "nope"), 600), 0)


# ======================================================================
# ③ main.py 接线
# ======================================================================
def part_c_wiring() -> None:
    print("\n--- ③ main.py 接线 ---")
    check_true("导入了 local_pub", "from .core import local_pub" in MAIN)
    check_true("import 顶格（缩进错会整份加载不了）",
               "\nfrom .core import local_pub\n" in MAIN)
    check_true("有 _QQ_IMAGE_FILE_TYPE 常量", "_QQ_IMAGE_FILE_TYPE = 1" in MAIN)

    check_true("定义了 _send_local_image",
               "async def _send_local_image(" in MAIN)
    check_true("定义了 _upload_qq_media_by_url",
               "async def _upload_qq_media_by_url(" in MAIN)
    check_true("定义了 _qq_upload_target",
               "def _qq_upload_target(" in MAIN)
    check_true("base64 路保留（降级兜底）",
               "async def _upload_qq_media(" in MAIN
               and '"file_data": base64.b64encode(data).decode("ascii")' in MAIN)

    # 两个上传方法必须分别用对字段
    url_fn = MAIN.split("async def _upload_qq_media_by_url(")[1].split("async def _send_local_image(")[0]
    check_true("by_url 用的是 url 字段", '"url": url,' in url_fn)
    check("by_url 里不该出现 file_data", '"file_data"' in url_fn, False)

    # 群 / 私聊两条 route 都要在
    tgt = MAIN.split("def _qq_upload_target(")[1].split("async def _qq_upload_request(")[0]
    check_true("群聊 route", "/v2/groups/{group_openid}/files" in tgt)
    check_true("私聊 route", "/v2/users/{openid}/files" in tgt)

    # 开关：留空即关闭
    sli = MAIN.split("async def _send_local_image(")[1].split("async def _send_qq_voice(")[0]
    check_true("先判是不是官机", "_is_qq_official(event)" in sli)
    check_true("读 localPubBaseUrl", 'conf_get("plugin.localPubBaseUrl"' in sli)
    check_true("base 为空直接 return False", "if not local_pub.normalize_base(base):" in sli)
    check_true("走的是 url 上传", "_upload_qq_media_by_url(" in sli)
    check_true("用 _send_qq_payload 发富媒体",
               '"msg_type": 7,' in sli and '"media": {"file_info": file_info}' in sli)

    # 5 个调用点
    n = MAIN.count("await self._send_local_image(event, png)")
    check("5 个本地图调用点都接了", n, 5)
    check_true("cookie 状态图接了", "render_cookie_status(rows, stamp, bg_api)" in MAIN)
    check_true("服务状态图接了",
               "if not await self._send_local_image(event, png):\n"
               "                yield event.chain_result([Comp.Image.fromBytes(png)])\n"
               "            return\n"
               "        yield event.plain_result(self._service_text(info))" in MAIN)
    check_true("点歌列表图接了",
               "if await self._send_local_image(event, png):\n                    return" in MAIN)
    check_true("网易云二维码接了",
               "if not await self._send_local_image(event, png):\n"
               "                # 用 fromBytes（base64），不落临时文件" in MAIN)
    check_true("菜单兜底图接了",
               "elif not await self._send_local_image(event, png):" in MAIN)

    # 原来的 base64 兜底一个都不能少
    check("原 base64 兜底还剩 5 处",
          MAIN.count("yield event.chain_result([Comp.Image.fromBytes(png)])"), 5)


# ======================================================================
# ④ schema
# ======================================================================
def part_d_schema() -> None:
    print("\n--- ④ schema ---")
    import json

    items = json.loads(SCHEMA)["plugin"]["items"]
    for k, t, dv in (("localPubBaseUrl", "string", ""),
                     ("localPubDir", "string", "/AstrBot/data/rconsole_pub"),
                     ("localPubTtl", "int", 600)):
        check_true(f"{k} 在 schema 里", k in items)
        if k in items:
            check(f"{k} 类型", items[k].get("type"), t)
            check(f"{k} 默认值", items[k].get("default"), dv)
    check("默认是关闭的（base 默认空）",
          items.get("localPubBaseUrl", {}).get("default"), "")

    # 源文件里也要有实测数据（别把知识丢了）
    check_true("docstring 记了实测对比",
               "7.39s" in PUB_SRC and "3.47s" in PUB_SRC)
    check_true("docstring 点明了适配器只有 base64",
               "file_data" in PUB_SRC and "没有 url 字段" in PUB_SRC)


if __name__ == "__main__":
    part_a_helpers()
    part_b_publish()
    part_c_wiring()
    part_d_schema()
    print()
    print(f"===== {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)
