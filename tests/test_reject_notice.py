"""「按规则拒绝」必须告知用户的离线回归测试（v1.6.3）。

**背景**：用户在群里发了一条 ``b23.tv`` 作品链接，机器人**一声不吭**，
只能人工去翻服务器日志，才看到::

    [WARN] 哔哩哔哩 解析失败: 视频时长 575s 超过配置上限 480s（可在配置里调整）

问题不在时长限制本身（那是用户自己配的），而在于**这类"解析出来了、
但按规则不发"的情况是完全静默的**：

- 它和「网络抖动 / 接口报错」被当成同一类，统一交给 ``plugin.reply_on_error``
  控制，而该选项默认关闭；
- 于是用户看到的现象是「发了链接，机器人没反应」，**不去翻日志根本无从排查**；
- 更糟的是，这类原因（时长上限）恰恰是用户自己改个配置就能解决的。

所以本测试锁定的核心不变量是：

    **``ResolveResult.reject()`` 标记的「按规则拒绝」，
    无论 ``reply_on_error`` 开还是关，都必须回一句原因。**

``fail()`` 的「真失败」行为保持不变：默认静默，开了 ``reply_on_error`` 才提示。

跑法::

    python tests/test_reject_notice.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 用**包名**导入（astrbot_plugin_rconsole.xxx）：插件内部大量 `from ..core.x
# import`，只有作为包被导入时相对导入才成立。
sys.path.insert(0, str(_ROOT.parent))

# 本地没有 AstrBot 运行环境时桩掉日志器，**必须在 import 插件模块之前**完成。
if "astrbot" not in sys.modules:
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

_FAILED: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}\n       期望: {want!r}\n       实际: {got!r}")
        _FAILED.append(label)


def check_true(label: str, cond: bool) -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}")
        _FAILED.append(label)


# ==========================================================================
# A 部分：纯逻辑（不需要 main.py，也不需要 AstrBot 运行时）
# ==========================================================================


def part_a_result_semantics() -> None:
    """``reject()`` / ``fail()`` / ``ok()`` 三者的语义必须分得开。"""
    print("\n[A] ResolveResult 的 rejected 语义")
    from astrbot_plugin_rconsole.platforms.base import ResolveResult

    rej = ResolveResult.reject("哔哩哔哩", "时长超上限")
    check_true("reject(): success=False", rej.success is False)
    check_true("reject(): rejected=True", rej.rejected is True)
    check("reject(): 保留原因文案", rej.error, "时长超上限")

    bad = ResolveResult.fail("哔哩哔哩", "接口挂了")
    check_true("fail(): rejected 默认 False", bad.rejected is False)

    good = ResolveResult.ok("哔哩哔哩", videos=["https://x/y.mp4"])
    check_true("ok(): rejected 默认 False", good.rejected is False)
    check_true("ok(): has_media=True", good.has_media is True)


def part_a_fmt_duration() -> None:
    """时长文案要给人看，不能把裸秒数丢出去。"""
    print("\n[A] _fmt_duration 可读化")
    from astrbot_plugin_rconsole.platforms.bilibili import _fmt_duration

    check("575 -> 9分35秒", _fmt_duration(575), "9分35秒")
    check("480 -> 8分0秒", _fmt_duration(480), "8分0秒")
    check("45 -> 45秒", _fmt_duration(45), "45秒")
    check("3600 -> 1小时0分0秒", _fmt_duration(3600), "1小时0分0秒")
    check("3725 -> 1小时2分5秒", _fmt_duration(3725), "1小时2分5秒")
    check("0 -> 0秒", _fmt_duration(0), "0秒")


def part_a_bili_reject() -> None:
    """B 站时长超限必须走 reject（而不是 fail），且文案是可读时长。"""
    print("\n[A] B 站时长超限 -> reject()")
    import astrbot_plugin_rconsole.platforms.bilibili as bili

    class Ctx:
        def conf(self, key, default=None):
            if key == "bili.biliDuration":
                return 480
            return default

        def cookie(self, platform):
            return ""

        def tool(self, name):
            return None

        async def llm(self, prompt):
            return ""

    async def fake_fetch_json(url, **kw):
        # 575 秒 —— 就是用户实际踩到的那条（b23.tv/jZfg2SZ）
        return {
            "code": 0,
            "data": {
                "title": "测试视频",
                "desc": "",
                "pic": "",
                "owner": {"name": "测试UP"},
                "duration": 575,
                "aid": 1,
                "pages": [{"cid": 123, "part": ""}],
            },
        }

    orig = bili.fetch_json
    bili.fetch_json = fake_fetch_json
    try:
        res = asyncio.run(
            bili.resolve_bilibili("https://www.bilibili.com/video/BV1xx411c7mD", Ctx())
        )
    finally:
        bili.fetch_json = orig

    check_true("时长超限: success=False", res.success is False)
    check_true("时长超限: rejected=True（关键）", res.rejected is True)
    check_true(
        "时长超限: 文案是可读时长而不是裸秒数",
        "9分35秒" in (res.error or "") and "575s" not in (res.error or ""),
    )
    check_true("时长超限: 文案里带着上限值", "8分0秒" in (res.error or ""))
    check_true("时长超限: 文案指明可调整", "biliDuration" in (res.error or ""))
    # ---- 下面几条是 v1.6.4 的核心：不能只回一句「超时长」就完事 ----
    check("时长超限: 带上了标题", res.title, "测试视频")
    check("时长超限: 带上了 UP 主", res.author, "测试UP")
    check(
        "时长超限: extra 里给出了作品页链接（关键）",
        (res.extra or {}).get("web_url"),
        "https://www.bilibili.com/video/BV1xx411c7mD",
    )
    check_true(
        "时长超限: 链接是作品页而不是带签名的 CDN 媒体直链",
        "bilibili.com/video/" in str((res.extra or {}).get("web_url"))
        and "upgcxcode" not in str((res.extra or {}).get("web_url")),
    )


def part_a_source_guard() -> None:
    """静态锁死：main.py 必须让 rejected 结果带着链接发出去。"""
    print("\n[A] main.py 源码不变量")
    src = (_ROOT / "main.py").read_text(encoding="utf-8")

    check_true("main.py 里用到了 result.rejected", "result.rejected" in src)
    check_true(
        "rejected 分支与 has_media 分支都走 _render_text_only",
        src.count("async for item in self._render_text_only(event, result):") >= 2,
    )
    check_true(
        "文本渲染支持作品页链接（web_url）",
        'result.extra.get("web_url")' in src and "👉 观看地址：" in src,
    )
    check_true(
        "日志区分「按规则未发送」与「解析失败」",
        "按规则未发送" in src and "解析失败" in src,
    )
    # 时长限制那处必须用 reject 而不是 fail，且要把链接带出来
    bsrc = (_ROOT / "platforms" / "bilibili.py").read_text(encoding="utf-8")
    check_true(
        "B 站时长超限用的是 ResolveResult.reject(",
        "ResolveResult.reject(" in bsrc,
    )
    check_true(
        "B 站 reject 时带上了 web_url（作品页链接）",
        '"web_url": watch_url' in bsrc,
    )
    check_true(
        "B 站 reject 时带上了 title / author",
        "title=title," in bsrc and "author=author," in bsrc,
    )


# ==========================================================================
# B 部分：_dispatch 的真实行为（需要桩掉 AstrBot 运行时）
# ==========================================================================


def part_b_dispatch_notice() -> None:
    """核心行为：rejected 一定提示，fail 看 reply_on_error。"""
    print("\n[B] _dispatch 的提示行为")
    from astrbot_plugin_rconsole.platforms.base import ResolveResult

    # ---- 桩 AstrBot 运行时（照 main.py 实际 import 的东西来）----
    si = types.SimpleNamespace
    mc = types.ModuleType("astrbot.api.message_components")
    for name in ("Plain", "Image", "Video", "Node", "Nodes", "Music", "Record"):
        setattr(mc, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    _Comp = type("MessageChain", (), {"__init__": lambda self, *a: None})

    ev_mod = types.ModuleType("astrbot.api.event")
    ev_mod.AstrMessageEvent = object
    ev_mod.MessageChain = _Comp
    ev_mod.filter = si(
        regex=lambda *a, **k: (lambda f: f),
        custom_filter=lambda *a, **k: (lambda f: f),
        permission_type=lambda *a, **k: (lambda f: f),
    )

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = object

    filt_mod = types.ModuleType("astrbot.api.event.filter")
    filt_mod.CustomFilter = object

    sys.modules["astrbot.api.message_components"] = mc
    sys.modules["astrbot.api.event"] = ev_mod
    sys.modules["astrbot.api.event.filter"] = filt_mod
    sys.modules["astrbot.api.star"] = star_mod

    import astrbot_plugin_rconsole.main as main_mod

    class FakeEvent:
        def __init__(self):
            self.chains = []

        @property
        def unified_msg_origin(self):
            return "test:GroupMessage:10086"

        def is_private_chat(self):
            return False

        def plain_result(self, text):
            self.chains.append(("plain", text))
            return ("plain", text)

        def chain_result(self, chain):
            self.chains.append(chain)
            return chain

        def track_temporary_local_file(self, p):
            pass

        def stop_event(self):
            pass

        def get_sender_name(self):
            return "测试用户"

        def get_sender_id(self):
            return "10001"

    CONFIG = {
        "plugin.enable": True,
        "plugin.only_group": False,
        "plugin.allow_multiple_links": False,
        "plugin.stop_on_match": True,
        "plugin.enabled_platforms": [r.key for r in main_mod.AUTO_RULES],
    }

    class FakePlugin(main_mod.Main):
        """继承 Main 但跳过 __init__（真实 __init__ 要 Context / 配置对象）。"""

        def __init__(self):  # noqa: D107 - 故意不调 super().__init__
            self._overrides = {}

        def conf_get(self, key, default=None):
            if key in self._overrides:
                return self._overrides[key]
            return CONFIG.get(key, default)

        def _get_cached(self, url):
            return None

        def _cache_result(self, url, result):
            pass

    URL = "https://www.bilibili.com/video/BV1xx411c7mD"
    TEXT = f"看看这个 {URL}"

    async def run_case(fake_result, overrides):
        """跑一次 _dispatch，返回 yield 出来的消息文本列表。"""
        plugin = FakePlugin()
        plugin._overrides = dict(overrides)

        async def fake_call(name, link, ctx, platform_name):
            return fake_result

        orig_call = main_mod.call
        main_mod.call = fake_call
        try:
            ev = FakeEvent()
            async for _ in plugin._dispatch(ev, TEXT):
                pass
        finally:
            main_mod.call = orig_call

        return [c[1] for c in ev.chains if isinstance(c, tuple) and c[0] == "plain"]

    REJECTED = ResolveResult.reject(
        "哔哩哔哩",
        "视频时长 9分35秒 超过上限 8分0秒，未下载（可在插件配置里调整 biliDuration）",
        title="东尼爆改麦晓雯！男人也可以这么美丽吗？！",
        author="流萤Zz",
        extra={"web_url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    )
    FAILED = ResolveResult.fail("哔哩哔哩", "视频信息接口请求失败: 连接超时")

    # ---- 1) 按规则拒绝 + reply_on_error 关闭 -> 仍然必须提示 ----
    texts = asyncio.run(run_case(REJECTED, {"plugin.reply_on_error": False}))
    check_true(
        "rejected + reply_on_error=False: 仍然回了提示（关键）",
        any("9分35秒" in t for t in texts),
    )
    joined = "\n".join(texts)
    check_true("rejected: 输出里有标题", "东尼爆改麦晓雯" in joined)
    check_true("rejected: 输出里有 UP 主", "流萤Zz" in joined)
    check_true(
        "rejected: 输出里有可点的作品链接（关键）",
        "https://www.bilibili.com/video/BV1xx411c7mD" in joined,
    )
    check_true(
        "rejected: 原因行带 ⏱️ 前缀（和普通备注区分开）",
        "⏱️" in joined,
    )

    # ---- 2) 按规则拒绝 + reply_on_error 打开 -> 也提示 ----
    texts = asyncio.run(run_case(REJECTED, {"plugin.reply_on_error": True}))
    check_true(
        "rejected + reply_on_error=True: 照常提示",
        any("9分35秒" in t for t in texts),
    )

    # ---- 3) 真失败 + reply_on_error 关闭 -> 静默（保持原行为）----
    texts = asyncio.run(run_case(FAILED, {"plugin.reply_on_error": False}))
    check("fail + reply_on_error=False: 静默（保持原行为）", texts, [])

    # ---- 4) 真失败 + reply_on_error 打开 -> 提示 ----
    texts = asyncio.run(run_case(FAILED, {"plugin.reply_on_error": True}))
    check_true(
        "fail + reply_on_error=True: 提示",
        any("连接超时" in t for t in texts),
    )


def main() -> int:
    print("=" * 72)
    print("「按规则拒绝」必须告知用户 —— 回归测试")
    print("=" * 72)

    part_a_result_semantics()
    part_a_fmt_duration()
    part_a_bili_reject()
    part_a_source_guard()
    part_b_dispatch_notice()

    print("\n" + "=" * 72)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：")
        for f in _FAILED:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
