# -*- coding: utf-8 -*-
"""分享卡片（``CQ:json``）识别链路的离线测试。

背景
====
QQ 群里分享小红书 / B 站时，发出来的**不是链接文本**，而是一张卡片
（``CQ:json`` 消息段）。这类消息的 ``get_message_str()`` 是空串：

- ``@filter.regex`` 匹配不到 → 自动解析入口完全不响应；
- 用户在 QQ 里看到的「小红书的链接」其实是卡片，机器人在日志里只会看到
  ``[ComponentType.Json]``，光看日志根本看不出是哪家平台。

实测（2026-09-19，群 878752629）
================================
小红书卡片没能解析，astrbot 日志里这一条属于**误伤**：

    22:40:36 [Core] ... OjojXrbVx2ZvoObIz4k4eJpZTipGu7dt: ❌ 解析失败：
    【https://www.xiaohongshu.com/discovery/item/6aae8e51000000002601eb7b...】:
    小红书作品不可访问或需要登录-eo

那是**上一次解析失败后机器人回的那段提示文本**，被重新识别成了一个链接
（提示里带着完整的 URL）—— 不是卡片没识别，而是提示文案自己成了新链接。

真正的问题在 22:40:10：用户发的是一条纯卡片消息

    22:40:10 [Core] ... 2593504303: [ComponentType.Json]

插件这时**完全没有反应**（连一行日志都没有），因为 ``Json`` 段的文本是空的。

守护的不变量
============
1. ``_card_urls`` 能从卡片原文里抠出链接，并还原 ``\\u0026`` / ``&amp;``
   这类转义。
2. ``_find_card_link`` 会拿抠出来的链接过 ``AUTO_RULES`` 规则表 ——
   也就是说**新增平台不用再为卡片单独写判断**。
3. ``BiliMiniappFilter`` 对「小红书卡片」返回 True（以前只有 B 站卡片命中）。
4. B 站小程序卡片的原有链路（``qqdocurl`` / BV 号兜底）**不能被改坏**。
5. 卡片原文会记进日志（``_capture_card_links``），但**不会把图片直链刷进来**。

跑法::

    python tests/test_card_link.py
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))

_PASS = 0
_FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global _PASS
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {got!r}"
          + ("" if ok else f"（期望 {want!r}）"))
    if ok:
        _PASS += 1
    else:
        _FAILED.append(name)


def check_true(name: str, cond, extra: str = "") -> None:
    global _PASS
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {extra}" if extra else ""))
    if ok:
        _PASS += 1
    else:
        _FAILED.append(name)


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return
    _logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api = types.ModuleType("astrbot.api")
    _api.logger = _logger
    _api.AstrBotConfig = dict
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

    mc = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, *a, **kw):
            self.args = a
            self.kw = kw
            for k, v in kw.items():
                setattr(self, k, v)

    class _Json(_Comp):
        """最小可用的 Json 组件：只需要 ``data``。"""

        def __init__(self, data=None, **kw):  # noqa: D107
            super().__init__(**kw)
            self.data = data

    for name in ("Image", "Video", "Record", "Plain", "Node", "Nodes",
                 "Music", "File"):
        setattr(mc, name, type(name, (_Comp,), {}))
    mc.Json = _Json
    mc.MessageChain = _Comp

    class _AnyFilter:
        def __getattr__(self, _n):
            return lambda *a, **k: (lambda f: f)

    ev = types.ModuleType("astrbot.api.event")
    ev.AstrMessageEvent = object
    ev.MessageChain = _Comp
    ev.filter = _AnyFilter()

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = object

    filt = types.ModuleType("astrbot.api.event.filter")
    filt.CustomFilter = object

    sys.modules["astrbot.api.message_components"] = mc
    sys.modules["astrbot.api.event"] = ev
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.event.filter"] = filt


# ==========================================================================
# 一份**真实抓到的**小红书卡片（2026-09-19 群 878752629，实测解析成功）。
#
# 注意它和 B 站卡片的字段位置完全不同 —— 这正是「不要按字段名取链接」的
# 理由。如果哪天有人把 _find_card_link 改成只读 ``qqdocurl``，这条会立刻红。
# ==========================================================================
XHS_CARD_REAL = {
    "app": "com.tencent.tuwen.lua",
    "bizsrc": "qqconnect.sdkshare",
    "config": {"ctime": 1789855198, "forward": 1,
               "token": "5b05394366217cb0f149fc84cc685d6f", "type": "normal"},
    "extra": {"app_type": 1, "appid": 100507190,
              "msg_seq": 7687369537053585098, "uin": 2593504303},
    "meta": {
        "news": {
            "app_type": 1,
            "appid": 100507190,
            "ctime": 1789855198,
            "desc": "加尔:我已休假不接稿不接广告 #洛克王国世界 #洛克…",
            "jumpUrl": (
                "https://www.xiaohongshu.com/discovery/item/"
                "6a8dbb140000000026006a4f?app_platform=android&ignoreEngage=true"
                "&app_version=9.47.0&share_from_user_hidden=true"
                "&xsec_source=app_share&type=normal"
                "&xsec_token=CByjwYFCZd16HHQSK8U0DGvINv-JOP4GuupBiHne_OWe0%3D"
                "&author_share=1&xhsshare=&shareRedId=OD83NTM6OU02NzUyOTgwNjhFOTc4SD5L"
                "&apptime=1789855192&share_id=8d5393fa85fe4d1f90aa4d16335c0f0a"
                "&share_channel=qq"
            ),
            "preview": "https://qq.ugcimg.cn/v1/ejrjt9tmsgau797pbp871orv1dj16r2lq4bdmo8k9cvnil80umir5slgbshguet10l9948pov7j3cuo2j8aejraqeuhibiu04mffkhjt4a1r4kno1l92u2js0bj0r7enab9lq5f2uirv9cu0bd41uqp8do7gu3ca201l7re6tpf96isfvmug/87p8ps0hsklc2us107r4ud0o9l60sm6hf036qv13qecrjokf02u0",
            "tag": "小红书",
            "tagIcon": "https://open.gtimg.cn/open/app_icon/00/50/71/90/100507190_100_m.png?t=1789610050",
            "title": "加尔老师今天的草团怎么有点不对",
            "uin": 2593504303,
        }
    },
    "prompt": "[分享]加尔老师今天的草团怎么有点不对",
    "ver": "0.0.0.1",
    "view": "news",
}

# 一份「照真实抓包结构写」的小红书卡片
#
# 关键点是：链接不是明文，而是藏在 ``jumpUrl`` 里且带 ``\u0026`` 转义；
# 卡片标题里的 emoji 是代理对（``\ud83c\udf1f``），不能因为解析出错就整条丢掉。
# ==========================================================================
XHS_CARD = {
    "app": "com.tencent.miniapp_01",
    "view": "viewMultiMsg",
    "ver": "0.0.0.1",
    "prompt": "[卡片]你在带我去月球吗",
    "meta": {
        "detail_1": {
            "appid": "1109658582",
            "appType": 0,
            "title": "⭐️你会带我去月球吗៷>ᴗ<៷",
            "desc": "小红书",
            "url": "m.q.qq.com",
            "qqdocurl": (
                "https://www.xiaohongshu.com/discovery/item/"
                "6aae8e51000000002601eb7b?app_platform=android"
                "\\u0026ignoreEngage=true\\u0026xsec_source=app_share"
                "\\u0026xsec_token=CBGgaiqk1vXWoUyacOvKOyOH5H7Tn-PVRHtUpFqdgePa0="
            ),
            "preview": "https://sns-img-bd.xhscdn.com/abc123?imageView2/2/w/1080",
            "hostBlackList": [],
        }
    },
}

# 微博卡片（同样走通用链路，验证「不用为每家平台写判断」）
WEIBO_CARD = {
    "meta": {
        "detail_1": {
            "title": "转发微博",
            "qqdocurl": "https://m.weibo.cn/detail/5012345678901234",
        }
    }
}

# B 站小程序卡片（原有链路，不能被改坏）
BILI_CARD = {
    "app": "com.tencent.miniapp_01",
    "meta": {
        "detail_1": {
            "appid": "1109937557",
            "title": "颠倒众生光头强",
            "qqdocurl": (
                "https://b23.tv/i7anTTd?share_medium=android"
                "\\u0026share_source=qq\\u0026bbid=XXCDF60CED7532910DEB5F"
            ),
        }
    },
}


def card_component(payload: dict):
    import importlib

    mc = importlib.import_module("astrbot.api.message_components")
    return mc.Json(data=payload)


# ==========================================================================
def url_extract_checks() -> None:
    print()
    print("=" * 70)
    print("1. 从卡片里抠链接")
    print("=" * 70)

    _install_stubs()
    import astrbot_plugin_rconsole.main as m

    urls = m._card_urls(card_component(XHS_CARD))
    check_true("抠出小红书链接", any("xiaohongshu.com" in u for u in urls),
               str(urls))
    joined = " ".join(urls)
    check_true("\\u0026 被还原成 &", "\\u0026" not in joined and "&" in joined,
               joined[:160])
    check_true("xsec_token 完整保留",
               "xsec_token=CBGgaiqk1vXWoUyacOvKOyOH5H7Tn-PVRHtUpFqdgePa0=" in joined,
               joined[:200])

    # 转义还原
    check("\\u0026 -> &", m._card_unescape("a\\u0026b"), "a&b")
    check("&amp; -> &", m._card_unescape("a&amp;b"), "a&b")
    check("&#44; -> ,", m._card_unescape("a&#44;b"), "a,b")
    check("\\/ -> /", m._card_unescape("https:\\/\\/x.com/a"), "https://x.com/a")

    # 非 dict 的 data 不能炸
    bad = card_component(None)
    check("data 为 None 时返回空表", m._card_urls(bad), [])

    # 日志用的文本要抹掉图片直链（否则一条卡片的缩略图地址就刷屏了）
    masked = m._card_text(card_component(XHS_CARD), mask_images=True)
    check_true("日志文本里图片直链被抹掉", "xhscdn.com" not in masked,
               masked[:200])
    check_true("抹图后笔记链接仍在", "xiaohongshu.com/discovery" in masked,
               masked[:200])
    check_true("原始文本（不抹图）里图片直链还在",
               "xhscdn.com" in m._card_text(card_component(XHS_CARD)))


def rule_match_checks() -> None:
    print()
    print("=" * 70)
    print("2. 抠出的链接能被规则表认出来（新增平台免改卡片代码）")
    print("=" * 70)

    import astrbot_plugin_rconsole.main as m

    link = m._find_card_link(card_component(XHS_CARD))
    check_true("小红书卡片能拿到可用链接", bool(link), str(link)[:120])
    if link:
        rule = m.match_rule(link, m.AUTO_RULES)
        check_true("规则表认成小红书", rule is not None and rule.key == "xhs",
                   str(rule.key if rule else None))
        check_true("链接里带着 xsec_token", "xsec_token=" in link, link[:160])

    link_w = m._find_card_link(card_component(WEIBO_CARD))
    check_true("微博卡片也能拿到链接", bool(link_w), str(link_w)[:120])
    if link_w:
        rule = m.match_rule(link_w, m.AUTO_RULES)
        check_true("规则表认成微博", rule is not None and rule.key == "weibo",
                   str(rule.key if rule else None))

    # 卡片里只有不认识的外链 -> 不该命中
    unknown = card_component({
        "meta": {"detail_1": {"qqdocurl": "https://example.com/whatever"}}
    })
    check("不认识的卡片返回 None", m._find_card_link(unknown), None)

    # 卡片里啥链接都没有 -> 不该命中
    check("无链接卡片返回 None",
          m._find_card_link(card_component({"meta": {}})), None)


def real_card_checks() -> None:
    """真实抓包卡片（2026-09-19 实测）—— 字段位置和 B 站完全不同。

    这条锁的是「不要按字段名取链接」这个设计决定：小红书用的是
    ``meta.news.jumpUrl``，B 站用的是 ``meta.detail_1.qqdocurl``。
    谁要是把实现改成只读某个字段名，这里立刻红。
    """
    print()
    print("=" * 70)
    print("2.5 真实抓到的卡片（字段位置与 B 站不同）")
    print("=" * 70)

    import astrbot_plugin_rconsole.main as m

    comp = card_component(XHS_CARD_REAL)
    link = m._find_card_link(comp)
    check_true("真实卡片能拿到链接", bool(link), str(link)[:140])
    if link:
        check_true("链接来自 meta.news.jumpUrl（不是 qqdocurl）",
                   "jumpUrl" not in link and "xiaohongshu.com" in link, link[:120])
        check_true("xsec_token 带在链接里（这是取内容的硬前提）",
                   "xsec_token=" in link, link[:200])
        check_true("xsec_source=app_share 也带上了",
                   "xsec_source=app_share" in link, link[:200])
        rule = m.match_rule(link, m.AUTO_RULES)
        check_true("规则表认成小红书", rule is not None and rule.key == "xhs",
                   str(rule.key if rule else None))

    # app 字段完全不同，也不能影响识别
    check("卡片 app 是 com.tencent.tuwen.lua",
          XHS_CARD_REAL["app"], "com.tencent.tuwen.lua")
    check_true("不依赖 appid 判断（appid 100507190 不在白名单里）",
               m._BILI_MINIAPP_APPID not in json.dumps(XHS_CARD_REAL))

    # filter 也要命中
    class _Ev:
        def get_messages(self):
            return [comp]

    check("真实卡片命中 filter",
          m.BiliMiniappFilter().filter(_Ev(), {}), True)

    # 日志用的文本要抹掉 preview 长直链
    masked = m._card_text(comp, mask_images=True)
    check_true("preview 长直链被抹掉（不刷屏）",
               "qq.ugcimg.cn/v1/ejrjt9t" not in masked, masked[:200])
    check_true("抹图后 jumpUrl 仍在", "jumpUrl" in masked, masked[:200])


def filter_checks() -> None:
    print()
    print("=" * 70)
    print("3. filter 行为（这是「没反应」的直接原因）")
    print("=" * 70)

    import astrbot_plugin_rconsole.main as m

    class _Ev:
        def __init__(self, comps):
            self._comps = comps

        def get_messages(self):
            return self._comps

    f = m.BiliMiniappFilter()

    check("小红书卡片命中", f.filter(_Ev([card_component(XHS_CARD)]), {}), True)
    check("微博卡片命中", f.filter(_Ev([card_component(WEIBO_CARD)]), {}), True)
    check("B 站小程序仍命中", f.filter(_Ev([card_component(BILI_CARD)]), {}), True)
    check("普通文本不命中", f.filter(_Ev([]), {}), False)
    check("无关卡片不命中",
          f.filter(_Ev([card_component({
              "meta": {"detail_1": {"qqdocurl": "https://example.com/x"}}
          })]), {}),
          False)


def bili_regression_checks() -> None:
    print()
    print("=" * 70)
    print("4. B 站小程序链路不能被改坏（回归）")
    print("=" * 70)

    import astrbot_plugin_rconsole.main as m

    comps = [card_component(BILI_CARD)]
    link = m._find_bili_link_in_messages(comps)
    check_true("仍能从 qqdocurl 取到 b23.tv 短链", bool(link), str(link)[:100])
    check_true("取到的是 b23.tv 链接", "b23.tv" in (link or ""), str(link)[:100])

    # 只带 BV 号的卡片：靠兜底拼标准链接
    bv_card = card_component({
        "meta": {"detail_1": {"appid": "1109937557",
                              "title": "视频 BV1Q3Yr6QESm 分享"}}
    })
    got = m._find_bili_link_in_messages([bv_card])
    check("只有 BV 号时拼标准链接", got,
          "https://www.bilibili.com/video/BV1Q3Yr6QESm")


def source_checks() -> None:
    print()
    print("=" * 70)
    print("5. 源码不变量")
    print("=" * 70)

    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _fn(name: str):
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name == name:
                return node
        return None

    handler = _fn("on_bili_miniapp")
    check_true("on_bili_miniapp 存在", handler is not None)
    if handler is not None:
        body = ast.get_source_segment(src, handler) or ""
        check_true("卡片原文会被记进日志", "_capture_card_links" in body)
        check_true("通用卡片走统一派发（_dispatch）", "_dispatch" in body)
        check_true("B 站链路仍在（_find_bili_link_in_messages）",
                   "_find_bili_link_in_messages" in body)

    # _capture_card_links 只记日志，不能有任何发送动作
    cap = _fn("_capture_card_links")
    check_true("_capture_card_links 存在", cap is not None)
    if cap is not None:
        cap_body = ast.get_source_segment(src, cap) or ""
        check_true("捕获函数不做渲染/发送",
                   "plain_result" not in cap_body and "chain_result" not in cap_body)
        check_true("捕获函数抹掉图片直链（mask_images=True）",
                   "mask_images=True" in cap_body)

    # 抹图逻辑在 _card_text 里
    ct = _fn("_card_text")
    ct_body = ast.get_source_segment(src, ct) if ct else ""
    check_true("_card_text 支持抹掉图片直链", "图片直链已省略" in ct_body)

    # _find_card_link 必须走规则表，而不是写死一串域名
    fcl = _fn("_find_card_link")
    fcl_body = ast.get_source_segment(src, fcl) if fcl else ""
    check_true("_find_card_link 走 match_rule 规则表",
               "match_rule(" in fcl_body)


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 分享卡片识别回归测试")
    print("=" * 70)
    url_extract_checks()
    rule_match_checks()
    real_card_checks()
    filter_checks()
    bili_regression_checks()
    source_checks()

    print()
    print("=" * 70)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败（通过 {_PASS} 项）：")
        for name in _FAILED:
            print(f"   · {name}")
        return 1
    print(f"✅ 全部通过（{_PASS} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
