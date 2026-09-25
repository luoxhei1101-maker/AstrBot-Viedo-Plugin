#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""番剧（ep / ss）解析测试。

原版把番剧丢给 BBDown 下载（BBDown 自带 APP 端鉴权）；本移植版没有 BBDown，
改成「season 接口拿元信息 + 单集的 ``bvid`` / ``cid`` 走**普通视频接口**」。

绕这一下是为了躲**地区限制**：官方的 ``pgc/player/web/playurl`` 在大陆以外只给
60 秒试看 —— 实测同一集（罗小黑第1话）在洛杉矶服务器 ``dash.duration`` 只有 62 秒，
在广州家宽是 305 秒；而普通视频接口不吃这个限制。

锁住的东西：

- 番剧链接识别（ep / ss），且**不误判**普通 BV 链接 / 含 ep 字样的 BV 号
- 番剧 ``duration`` 是**毫秒** -> 秒的换算
- 三道闸门：未登录 / 超时长 / 未开「番剧直接解析」-> 全都只发信息不下载
- 放行时：时长上限走 ``biliBangumiDuration``（不是 8 分钟的 ``biliDuration``）
- 画质走「番剧独立画质」``biliBangumiResolution``
- 播放地址走普通接口 —— 源码里**不允许**出现 ``pgc/player/web/playurl``
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

from astrbot_plugin_rconsole.platforms.bilibili import (  # noqa: E402
    _EP_ID_RE,
    _SS_ID_RE,
    _as_int,
    _bangumi_gate,
    _ms_to_seconds,
    _resolve_qn,
)

PASS = FAIL = 0

SRC = (Path(__file__).resolve().parent.parent / "platforms" / "bilibili.py").read_text(
    encoding="utf-8"
)


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {msg}")
    else:
        FAIL += 1
        print(f"[FAIL] {msg}")


class Ctx:
    """只实现 ResolverContext 里 resolver 用到的那两个方法。"""

    def __init__(self, **conf):
        self._conf = conf

    def conf(self, key, default=None):
        return self._conf.get(key, default)

    def cookie(self, platform):
        return self._conf.get("_cookie", "")


def make_pgc(**over):
    base = {
        "ep_id": 32374,
        "bvid": "BV1fx411K7F1",
        "cid": 34441989712,
        "title": "《罗小黑战记》第1话 喵",
        "season_title": "罗小黑战记",
        "ep_title": "第1话 喵",
        "cover": "https://i0.hdslb.com/x.jpg",
        "desc": "猫妖小黑……",
        "duration": 304,
        "web_url": "https://www.bilibili.com/bangumi/play/ep32374",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1) 链接识别
# ---------------------------------------------------------------------------
def test_link_detect() -> None:
    print("\n--- 番剧链接识别 ---")
    eps = [
        "https://www.bilibili.com/bangumi/play/ep6364375",
        "https://b23.tv/ep6364375",
        "https://www.bilibili.com/bangumi/play/ep32374?from=search",
        "https://m.bilibili.com/bangumi/play/ep32374",
    ]
    for u in eps:
        m = _EP_ID_RE.search(u)
        check(bool(m), f"识别 ep: {u}")

    ss = [
        "https://www.bilibili.com/bangumi/play/ss1733",
        "https://b23.tv/ss1733",
    ]
    for u in ss:
        m = _SS_ID_RE.search(u)
        check(bool(m), f"识别 ss: {u}")

    # 短链展开后是 bangumi 页面（实测 b23.tv/0S1Fyl0 -> .../play/ep6364375）
    expanded = (
        "https://www.bilibili.com/bangumi/play/ep6364375?from_spmid=0.0.0.0&unique_k=0S1Fyl0"
    )
    check(bool(_EP_ID_RE.search(expanded)), "短链展开后的 bangumi 页能认出 ep")

    # **不能误判**的情况
    normal = [
        "https://www.bilibili.com/video/BV1Deht6rEpZ",
        "https://www.bilibili.com/video/BV1ep411c7mD",  # BV 号里含 ep 字样
        "https://b23.tv/abc123",
        "https://www.bilibili.com/video/av170001",
        "https://www.bilibili.com/read/cv1234567",
    ]
    for u in normal:
        check(
            not _EP_ID_RE.search(u) and not _SS_ID_RE.search(u),
            f"不误判成番剧: {u}",
        )


# ---------------------------------------------------------------------------
# 2) 数值换算
# ---------------------------------------------------------------------------
def test_convert() -> None:
    print("\n--- duration 毫秒 -> 秒 ---")
    # season 接口这个字段固定是毫秒，无条件除 1000
    check(_ms_to_seconds(304458) == 304, "304458ms -> 304s")
    check(_ms_to_seconds(61438) == 61, "61438ms -> 61s（别靠数值大小猜单位）")
    check(_ms_to_seconds(14400000) == 14400, "4 小时(ms) -> 14400s")
    check(_ms_to_seconds(0) == 0, "0 -> 0")
    check(_ms_to_seconds(None) == 0, "None -> 0")
    check(_ms_to_seconds("") == 0, "空串 -> 0")
    check(_ms_to_seconds("304458") == 304, "字符串毫秒也能转")

    check(_as_int(None) == 0, "_as_int(None) == 0")
    check(_as_int("32") == 32, "_as_int 能吃字符串")
    check(_as_int("abc") == 0, "_as_int 吃坏值不炸")


# ---------------------------------------------------------------------------
# 3) 三道闸门
# ---------------------------------------------------------------------------
def test_gate() -> None:
    print("\n--- 番剧闸门 ---")

    # ① 未登录 -> 拦
    r = _bangumi_gate(make_pgc(), Ctx(), "")
    check(r is not None and r.rejected, "未登录 -> 拒绝")
    check(bool(r and "登录" in (r.error or "")), "未登录的提示里点明要登录")
    check(bool(r and r.title and r.extra.get("web_url")), "拒绝时带上作品信息与链接")

    # ② 超时长（默认上限 1800s）-> 拦
    ctx = Ctx(**{"bili.biliBangumiDuration": 1800, "bili.biliBangumiDirect": True})
    r = _bangumi_gate(make_pgc(duration=2000), ctx, "SESSDATA=x")
    check(r is not None and r.rejected, "单集 2000s > 上限 1800s -> 拒绝")
    check(bool(r and "biliBangumiDuration" in (r.error or "")), "超时长提示里给出可改的配置项名")

    # 超时长与开关同时成立时，**超时长优先**（原版也是先判超限就 return）
    r = _bangumi_gate(
        make_pgc(duration=2000),
        Ctx(**{"bili.biliBangumiDuration": 1800, "bili.biliBangumiDirect": False}),
        "SESSDATA=x",
    )
    check(
        bool(r and "超过上限" in (r.error or "")),
        "超时长优先于「未开开关」",
    )

    # ③ 没开「番剧直接解析」-> 拦
    r = _bangumi_gate(
        make_pgc(), Ctx(**{"bili.biliBangumiDuration": 1800, "bili.biliBangumiDirect": False}),
        "SESSDATA=x",
    )
    check(r is not None and r.rejected, "未开 biliBangumiDirect -> 拒绝（只发信息）")
    check(bool(r and "biliBangumiDirect" in (r.error or "")), "提示里给出开关名")

    # ④ 全通过 -> 放行
    r = _bangumi_gate(
        make_pgc(), Ctx(**{"bili.biliBangumiDuration": 1800, "bili.biliBangumiDirect": True}),
        "SESSDATA=x",
    )
    check(r is None, "登录 + 未超时长 + 开关开启 -> 放行")

    # ⑤ 上限 0 = 不限
    r = _bangumi_gate(
        make_pgc(duration=99999),
        Ctx(**{"bili.biliBangumiDuration": 0, "bili.biliBangumiDirect": True}),
        "SESSDATA=x",
    )
    check(r is None, "biliBangumiDuration=0 视为不限制")

    # ⑥ 配置缺失时用默认 1800
    r = _bangumi_gate(
        make_pgc(duration=1700), Ctx(**{"bili.biliBangumiDirect": True}), "SESSDATA=x"
    )
    check(r is None, "配置缺失时默认上限 1800s（1700s 放行）")


# ---------------------------------------------------------------------------
# 4) 画质
# ---------------------------------------------------------------------------
def test_quality() -> None:
    print("\n--- 画质选择 ---")
    # 番剧走 biliBangumiResolution（下拉索引 9 = 480P = qn 32）
    qn, name = _resolve_qn(Ctx(**{"bili.biliBangumiResolution": "9"}), bangumi=True)
    check(qn == 32, f"番剧独立画质 索引9 -> qn=32 480P（实际 qn={qn}）")

    # 普通视频不该受番剧画质影响
    ctx = Ctx(**{"bili.biliBangumiResolution": "9", "bili.biliResolution": "3"})
    qn_v, _ = _resolve_qn(ctx, bangumi=False)
    qn_b, _ = _resolve_qn(ctx, bangumi=True)
    check(qn_v == 120, f"普通视频用 biliResolution（索引3 -> 4K qn=120，实际 {qn_v}）")
    check(qn_b == 32, f"番剧用 biliBangumiResolution（实际 {qn_b}）")
    check(qn_v != qn_b, "两条画质链路互不干扰")

    # 番剧没配 -> 退回普通视频那套
    qn_fb, _ = _resolve_qn(Ctx(**{"bili.biliResolution": "3"}), bangumi=True)
    check(qn_fb == 120, f"番剧没配画质时退回 biliResolution（实际 {qn_fb}）")


# ---------------------------------------------------------------------------
# 5) 源码不变量
# ---------------------------------------------------------------------------
def test_source_invariants() -> None:
    print("\n--- 源码不变量 ---")
    check("_EP_RE" not in SRC, "旧的 _EP_RE 已删除（避免和新正则并存）")
    check(
        '"https://api.bilibili.com/pgc/player' not in SRC,
        "代码里没有官方番剧播放接口的 URL 常量（大陆外只给 60s 试看）",
    )
    check(
        SRC.count("pgc/player/web/playurl") <= 3,
        "官方番剧播放接口只出现在解释性注释里",
    )
    check("bili/play/ep" in SRC or "bangumi/play/ep" in SRC, "番剧 Referer 用 bangumi 页")
    check(
        "max_duration = 0 if pgc else" in SRC,
        "番剧跳过普通视频的时长上限（另有 biliBangumiDuration）",
    )
    check(
        "_resolve_qn(ctx, bangumi=bool(pgc))" in SRC,
        "番剧走番剧画质分支",
    )
    check(
        SRC.count("_bangumi_gate(") >= 2,
        "_bangumi_gate 既定义也在主流程里被调用",
    )
    check(
        "if pgc:\n        blocked = _bangumi_gate(pgc, ctx, cookie)" in SRC,
        "主流程里番剧先过闸门",
    )
    check(
        'if "bili.biliBangumiDirect", False' in SRC.replace("ctx.conf(", "ctx.conf(")
        or "bili.biliBangumiDirect" in SRC,
        "开关读的是 bili.biliBangumiDirect",
    )
    # 番剧的 view 接口返回不可靠，元信息必须来自 season
    check(
        "番剧的 view 接口返回不可靠" in SRC,
        "注释交代了为什么番剧不走 view 接口",
    )
    # 短链只展开一次
    check(
        'if any(h in link for h in ("b23.tv", "bili2233.cn"))' in SRC,
        "主流程开头统一展开短链（避免重复请求）",
    )


def main() -> int:
    test_link_detect()
    test_convert()
    test_gate()
    test_quality()
    test_source_invariants()
    print(f"\n{'=' * 52}\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
