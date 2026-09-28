#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地图 → 公网直链（``core/local_pub.py`` + main.py 的接线）。

**背景**（2026-09-27 实测）：

官机发**本地生成的图**时，AstrBot 适配器只有 base64 一条路
（``upload_group_and_c2c_image`` 的 payload **没有 url 字段**），
对「境外机器 → 腾讯境内 API」这条链路，把整个文件传上去就是最慢的一环：

    base64 ： 97KB  → 7.39s / 14.88s
    url    ：784KB  → 3.47s     ← 体积大 8 倍反而快一倍多

URL 走**白嫖 AstrBot 自己的 WebUI 端口**：它的认证中间件只拦 ``/api``，
WebUI 前端目录是静态目录 → 往里面写图就能匿名访问（用户零配置）。

锁住的东西：

- 三种模式优先级：自定义前缀 > AstrBot 访问地址 > 自动探测
- 用户粘了完整图片链接时，必须**削成主机前缀**（否则拼出双份路径）
- 文件名按内容哈希 → 同图不重复落盘
- **cleanup 绝不能删 WebUI 的前端资源**（静态目录里还住着前端）
- 5 个本地图调用点都要接上
- ``_upload_qq_media`` 的 base64 路**必须保留**（降级兜底）
- **``localPubEnabled`` 默认关闭**（2026-09-28）：这条路的硬前提是「腾讯能从公网
  访问到这台机器」，家用机 / NAT / 内网不满足 —— 而不满足时探测阶段看不出来，
  只会让每次发图白等一次上传超时。家用的部署形态必须留在 hint 里。
- **失败熔断**：连续 2 次「腾讯取不到图」就自动停用 30 分钟，改走普通上传；
  用户改了地址（配置指纹变化）立刻解除。
"""

from __future__ import annotations

import asyncio
import json
import os
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
    print("\n--- ① 归一化 / 削路径 / 后缀 / 文件名 ---")
    nb = local_pub.normalize_base
    check("去掉结尾斜杠", nb("http://1.2.3.4:6185/"), "http://1.2.3.4:6185")
    check("多个斜杠也只留一个", nb("http://1.2.3.4:6185///"), "http://1.2.3.4:6185")
    check("去空白", nb("  http://a.com  "), "http://a.com")
    check("空串还是空串", nb(""), "")
    check("None 也安全", nb(None), "")

    sap = local_pub.strip_asset_path
    check("纯主机保留", sap("http://1.2.3.4:6185"), "http://1.2.3.4:6185")
    check("带尾斜杠削掉", sap("http://1.2.3.4:6185/"), "http://1.2.3.4:6185")
    check("粘了完整图片链接也要削成主机",
          sap("http://1.2.3.4:6185/rconsole-pub/abc123.png"), "http://1.2.3.4:6185")
    check("粘了子目录也削掉",
          sap("https://bot.example.com/rconsole-pub"), "https://bot.example.com")
    check("域名带路径也削", sap("https://bot.example.com/a/b/c.jpg"), "https://bot.example.com")
    check("没有 scheme 时原样（允许只填 host:port）",
          sap("1.2.3.4:6185"), "1.2.3.4:6185")
    check("空串安全", sap(""), "")

    ss = local_pub.safe_suffix
    check(".png 保留", ss(".png"), ".png")
    check("PNG 转小写", ss(".PNG"), ".png")
    check("不带点也认", ss("jpg"), ".jpg")
    check("未知后缀 → .bin", ss(".exe"), ".bin")
    check(".py 不在白名单 → .bin", ss(".py"), ".bin")
    check("空 → .bin", ss(""), ".bin")

    cn = local_pub.content_name
    a, b, c = cn(b"hello", ".png"), cn(b"hello", ".png"), cn(b"hellp", ".png")
    check("同内容同名（天然去重）", a == b, True)
    check("不同内容不同名", a != c, True)
    check("后缀接上了", a.endswith(".png"), True)
    check("名字长度合理（24 hex + 后缀）", len(a), 28)

    check("子目录名固定", local_pub.SUBDIR, "rconsole-pub")
    check("TTL 默认 600", local_pub.DEFAULT_TTL, 600)
    check("上限 32MB", local_pub.MAX_BYTES, 32 * 1024 * 1024)


# ======================================================================
# ② resolve_target 的三种模式
# ======================================================================
def part_b_resolve() -> None:
    print("\n--- ② resolve_target：三种模式 ---")
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_resolve_cases(Path(td)))


async def _resolve_cases(td: Path) -> None:
    local_pub.reset_cache()

    # ① 自定义前缀：完全接管，**不再拼 SUBDIR**
    t = await local_pub.resolve_target(base_url="http://1.2.3.4:18900/", directory=str(td))
    check_true("自定义前缀模式返回了目标", t is not None)
    check("自定义前缀直接用（不拼子目录）", t.base_url, "http://1.2.3.4:18900")
    check("目录是 <dir>/rconsole-pub", t.directory, td / local_pub.SUBDIR)
    check("url_for 拼对", t.url_for("x.png"), "http://1.2.3.4:18900/x.png")

    # ② 手动 AstrBot 访问地址：只覆盖主机，**路径仍拼 SUBDIR**
    local_pub.reset_cache()
    t2 = await local_pub.resolve_target(public_url="https://bot.example.com", directory=str(td))
    check_true("手动地址模式返回了目标", t2 is not None)
    check("手动地址会自动拼 /rconsole-pub", t2.base_url,
          "https://bot.example.com/" + local_pub.SUBDIR)

    local_pub.reset_cache()
    t3 = await local_pub.resolve_target(
        public_url="http://1.2.3.4:6185/rconsole-pub/abcdef.png", directory=str(td))
    check("粘完整链接 → 削成主机再拼一次（不会双份）", t3.base_url,
          "http://1.2.3.4:6185/" + local_pub.SUBDIR)

    local_pub.reset_cache()
    t4 = await local_pub.resolve_target(
        base_url="http://win:1", public_url="http://lose:2", directory=str(td))
    check("两个都填时 base_url 优先", t4.base_url, "http://win:1")


# ======================================================================
# ③ publish_to / cleanup（真写盘）
# ======================================================================
def part_c_publish() -> None:
    print("\n--- ③ publish_to / cleanup（真写盘） ---")
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_pub_cases(Path(td)))


async def _pub_cases(td: Path) -> None:
    tgt = local_pub.PublishTarget(td, "http://h:1/rconsole-pub")

    check("空数据 → None", await local_pub.publish_to(b"", tgt), None)

    png = b"\x89PNG\r\n\x1a\n" + b"A" * 500
    url = await local_pub.publish_to(png, tgt, ttl=0)
    check_true("返回了 URL", url, str(url))
    check("URL 前缀正确", url.startswith("http://h:1/rconsole-pub/"), True)
    check("URL 以 .png 结尾", url.endswith(".png"), True)
    name = url.rsplit("/", 1)[1]
    fp = td / name
    check_true("文件真的落盘了", fp.exists())
    check("大小一致", fp.stat().st_size, len(png))
    check("内容一致", fp.read_bytes(), png)
    check("没有残留 .part", list(td.glob("*.part")), [])

    url2 = await local_pub.publish_to(png, tgt, ttl=0)
    check("同内容 URL 相同", url2, url)
    check("目录里只有 1 个文件", len(list(td.iterdir())), 1)

    url3 = await local_pub.publish_to(png + b"B", tgt, ttl=0)
    check_true("不同内容给出不同 URL", url3 and url3 != url)

    huge = b"x" * (local_pub.MAX_BYTES + 1)
    check("超上限 → None", await local_pub.publish_to(huge, tgt), None)

    # ---- cleanup 的关键安全性：**不能碰别人的文件** ----
    print("      〔cleanup 安全性〕")
    for fn in ("index.html", "app.abc123.js", "favicon.svg"):
        (td / fn).write_bytes(b"webui")
    (td / "assets").mkdir(exist_ok=True)
    (td / "assets" / "chunk.js").write_bytes(b"chunk")

    old = td / ("a" * 24 + ".png")
    old.write_bytes(b"old")
    past = time.time() - 9999
    os.utime(old, (past, past))
    probe = td / (local_pub._PROBE_PREFIX + "123.txt")
    probe.write_bytes(b"p")
    os.utime(probe, (past, past))

    removed = local_pub.cleanup(str(td), ttl=600)
    check("只删了 2 个自己的旧文件（1 图 + 1 探针）", removed, 2)
    check("旧图被删", old.exists(), False)
    check("探针残留被删", probe.exists(), False)
    for fn in ("index.html", "app.abc123.js", "favicon.svg"):
        check_true(f"**WebUI 资源没被误删：{fn}**", (td / fn).exists())
    check_true("assets 目录还在", (td / "assets").is_dir())
    check_true("assets 里的文件还在", (td / "assets" / "chunk.js").exists())

    check("cleanup 对不存在目录返回 0", local_pub.cleanup(str(td / "nope"), 600), 0)
    check("ttl=0 不清理", local_pub.cleanup(str(td), 0), 0)


# ======================================================================
# ④ main.py 接线
# ======================================================================
def part_d_wiring() -> None:
    print("\n--- ④ main.py 接线 ---")
    check_true("导入了 local_pub", "from .core import local_pub" in MAIN)
    check_true("import 顶格（缩进错会整份加载不了）",
               "\nfrom .core import local_pub\n" in MAIN)
    check_true("有 _QQ_IMAGE_FILE_TYPE 常量", "_QQ_IMAGE_FILE_TYPE = 1" in MAIN)

    check_true("定义了 _send_local_image", "async def _send_local_image(" in MAIN)
    check_true("定义了 _upload_qq_media_by_url", "async def _upload_qq_media_by_url(" in MAIN)
    check_true("定义了 _qq_upload_target", "def _qq_upload_target(" in MAIN)
    check_true("base64 路保留（降级兜底）",
               "async def _upload_qq_media(" in MAIN
               and '"file_data": base64.b64encode(data).decode("ascii")' in MAIN)

    url_fn = MAIN.split("async def _upload_qq_media_by_url(")[1].split("async def _send_local_image(")[0]
    check_true("by_url 用的是 url 字段", '"url": url,' in url_fn)
    check("by_url 里不该出现 file_data", '"file_data"' in url_fn, False)

    tgt = MAIN.split("def _qq_upload_target(")[1].split("async def _qq_upload_request(")[0]
    check_true("群聊 route", "/v2/groups/{group_openid}/files" in tgt)
    check_true("私聊 route", "/v2/users/{openid}/files" in tgt)

    sli = MAIN.split("async def _send_local_image(")[1].split("async def _send_qq_voice(")[0]
    check_true("先判是不是官机", "_is_qq_official(event)" in sli)
    check_true("有总开关 localPubEnabled（**默认 False**）",
               'conf_get("plugin.localPubEnabled", False)' in sli)
    check_true("取图失败记一笔熔断（别每次白等 25 秒）",
               "local_pub.note_transfer_failure()" in sli)
    check_true("取图成功清计数", "local_pub.note_transfer_success()" in sli)
    check_true("走 resolve_target", "await local_pub.resolve_target(" in sli)
    check_true("读 AstrBot 访问地址", 'conf_get("plugin.localPubPublicUrl"' in sli)
    check_true("读自定义前缀", 'conf_get("plugin.localPubBaseUrl"' in sli)
    check_true("读落盘目录", 'conf_get("plugin.localPubDir"' in sli)
    check_true("用 publish_to 落盘", "await local_pub.publish_to(" in sli)
    check_true("走的是 url 上传", "_upload_qq_media_by_url(" in sli)
    check_true("用 _send_qq_payload 发富媒体",
               '"msg_type": 7,' in sli and '"media": {"file_info": file_info}' in sli)

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

    check("原 base64 兜底还剩 5 处",
          MAIN.count("yield event.chain_result([Comp.Image.fromBytes(png)])"), 5)


# ======================================================================
# ⑤ schema + 源码里的知识
# ======================================================================
def part_e_schema() -> None:
    print("\n--- ⑤ schema ---")
    items = json.loads(SCHEMA)["plugin"]["items"]
    for k, t, dv in (("localPubEnabled", "bool", False),
                     ("localPubPublicUrl", "string", ""),
                     ("localPubBaseUrl", "string", ""),
                     ("localPubDir", "string", ""),
                     ("localPubTtl", "int", 600)):
        check_true(f"{k} 在 schema 里", k in items)
        if k in items:
            check(f"{k} 类型", items[k].get("type"), t)
            check(f"{k} 默认值", items[k].get("default"), dv)

    check_true("localPubPublicUrl 描述点明是「AstrBot 访问地址」",
               "AstrBot 访问地址" in items["localPubPublicUrl"]["description"])
    check_true("localPubPublicUrl 的 hint 提醒留空=自动探测",
               "留空 = 自动探测" in items["localPubPublicUrl"]["hint"])
    # ⚠️ 默认必须**关**（2026-09-28 改）：这功能的硬前提是「腾讯能从公网访问到
    # AstrBot 所在机器」，云服务器满足，家用机 / NAT / 内网不满足 —— 而不满足时
    # 探测阶段看不出来，只会让每次发图白等一次上传超时（最长 25 秒）。
    check_true("**localPubEnabled 默认关闭**（家用机不该默认开）",
               items["localPubEnabled"]["default"] is False)
    check_true("描述里点明「仅云服务器建议开启」",
               "云服务器" in items["localPubEnabled"]["description"])
    check_true("hint 写了「必须保持关闭」的场景",
               "必须保持关闭" in items["localPubEnabled"]["hint"])
    check_true("hint 点名家用的部署形态（家用电脑 / NAT）",
               "家用电脑" in items["localPubEnabled"]["hint"]
               and "NAT" in items["localPubEnabled"]["hint"])
    check_true("hint 说明「关掉只是慢一点，功能不受影响」",
               "功能完全不受影响" in items["localPubEnabled"]["hint"])
    check_true("hint 说明误开也有熔断兜底",
               "自动停用" in items["localPubEnabled"]["hint"])

    check_true("源码 docstring 记了实测对比", "7.39s" in PUB_SRC and "3.47s" in PUB_SRC)
    check_true("源码 docstring 点明适配器只有 base64",
               "file_data" in PUB_SRC and "没有 url 字段" in PUB_SRC)
    check_true("源码 docstring 写清了「白嫖 WebUI 端口」的原理",
               "auth_middleware" in PUB_SRC and 'startswith("/api")' in PUB_SRC)

    # 适用前提（2026-09-28 补）—— 这就是「默认关闭」的理由，必须留在文档里
    check_true("docstring 点明前提：机器得让腾讯访问得到",
               "家用电脑" in PUB_SRC and "公网 IP" in PUB_SRC)
    check_true("docstring 说明环境不对时探测阶段看不出来",
               "探测阶段是看不出来的" in PUB_SRC)
    check_true("docstring 记了两道防线（默认关 + 熔断）",
               "默认关闭" in PUB_SRC and "note_transfer_failure" in PUB_SRC)


# ======================================================================
# ⑥ 失败熔断
# ======================================================================
def part_f_trip() -> None:
    print("\n--- ⑥ 失败熔断（环境不可达时别每次白等） ---")
    local_pub.reset_cache()
    check("连续 2 次就熔断", local_pub.FAIL_STREAK_THRESHOLD, 2)
    check("熔断 30 分钟", local_pub.TRIP_SECONDS, 1800.0)
    check("初始没熔断", local_pub.is_tripped(""), False)
    check("初始计数 0", local_pub.failure_streak(), 0)

    with tempfile.TemporaryDirectory() as td:
        # 先正常解析一次（模拟「探测成功、拿到配置指纹」）
        t0 = asyncio.run(local_pub.resolve_target(
            base_url="http://1.2.3.4:18900", directory=str(td)))
        check_true("先成功解析一次（记下配置指纹）", t0 is not None)
        key = local_pub._CACHE_KEY
        check_true("配置指纹已记录", bool(key))

        local_pub.note_transfer_failure()
        check("失败 1 次还不熔断（可能只是抖动）", local_pub.is_tripped(key), False)
        check("计数 1", local_pub.failure_streak(), 1)

        local_pub.note_transfer_failure()
        check("**失败 2 次 → 熔断**", local_pub.is_tripped(key), True)
        check("计数 2", local_pub.failure_streak(), 2)

        # 熔断期里再解析：直接 None —— 不探测、不白等
        t1 = asyncio.run(local_pub.resolve_target(
            base_url="http://1.2.3.4:18900", directory=str(td)))
        check("**熔断期 resolve_target 直接返回 None**", t1, None)

        # 用户改了地址（指纹变）→ 立刻解除，给他重试机会
        t2 = asyncio.run(local_pub.resolve_target(
            base_url="http://9.9.9.9:1", directory=str(td)))
        check_true("改配置（指纹变）→ 解除熔断、重新可用", t2 is not None)

    # 成功一次就清零
    local_pub.note_transfer_success()
    check("成功后计数清零", local_pub.failure_streak(), 0)
    check("成功后解除熔断", local_pub.is_tripped("k"), False)

    # reset_cache 也清熔断
    local_pub._CACHE_KEY = "k"
    local_pub.note_transfer_failure()
    local_pub.note_transfer_failure()
    check("（重置前确实是熔断态）", local_pub.is_tripped("k"), True)
    local_pub.reset_cache()
    check("reset_cache 也清熔断", local_pub.is_tripped("k"), False)


if __name__ == "__main__":
    part_a_helpers()
    part_b_resolve()
    part_c_publish()
    part_d_wiring()
    part_e_schema()
    part_f_trip()
    print()
    print(f"===== {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)
