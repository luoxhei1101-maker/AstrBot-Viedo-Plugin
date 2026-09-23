"""「解析内容用聊天记录（合并转发）发送」的离线回归测试。

守护的不变量
============

1. **开关是唯一的判据**：``plugin.send_as_forward`` 为假时走原来的直发路径
   （简介一条、媒体一条条发）；为真时整条解析只产出**一条**消息链，且里面是
   ``Comp.Nodes``。
2. **节点顺序**：简介在最前，之后每个媒体各占一个节点。
3. **``show_desc`` 关掉时没有简介节点**，但媒体照发。
4. **一个媒体都没有时不硬发空壳聊天记录** —— 退回普通文字情报
   （一条只有文字的转发点开跟普通消息一样，还多一次点击）。
5. **转发节点里的视频必须走 ``_video_component``（base64）**，
   禁止 ``Comp.Video.fromFileSystem``（跨容器必 ENOENT —— 见
   ``test_album_send_path`` 里的说明）。

跑法::

    python tests/test_forward_send.py
"""

from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))


def _real_astrbot_available() -> bool:
    try:
        import astrbot.api.event  # noqa: F401
        import astrbot.api.message_components  # noqa: F401
        import astrbot.api.star  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


NEED_STUBS = not _real_astrbot_available()

if NEED_STUBS and "astrbot" not in sys.modules:
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


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {got!r}"
          + ("" if ok else f"（期望 {want!r}）"))
    if not ok:
        _FAILED.append(name)


def check_true(name: str, cond, extra: str = "") -> None:
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {extra}" if extra else ""))
    if not ok:
        _FAILED.append(name)


def _install_astrbot_stubs() -> None:
    if not NEED_STUBS:
        print("  (真实 AstrBot 环境，跳过打桩)")
        return

    mc = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, *a, **kw):
            self.args = a
            self.kw = kw
            for k, v in kw.items():
                setattr(self, k, v)

        @classmethod
        def fromBase64(cls, data, **_kw):
            return cls(file=f"base64://{len(data)}")

        @classmethod
        def fromURL(cls, url, **_kw):
            return cls(file=url)

        @classmethod
        def fromFileSystem(cls, path, **_kw):
            return cls(file=f"file:///{path}")

        @classmethod
        def fromBytes(cls, data, **_kw):
            return cls(file=f"bytes://{len(data)}")

        def __repr__(self) -> str:
            return f"{type(self).__name__}({self.kw.get('file') or self.args})"

    for name in ("Image", "Video", "Record", "Plain", "Node", "Nodes",
                 "Json", "Music", "File"):
        setattr(mc, name, type(name, (_Comp,), {}))
    mc.MessageChain = _Comp

    class _AnyFilter:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda f: f)

    ev_mod = types.ModuleType("astrbot.api.event")
    ev_mod.AstrMessageEvent = object
    ev_mod.MessageChain = _Comp
    ev_mod.filter = _AnyFilter()

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = object

    filt_mod = types.ModuleType("astrbot.api.event.filter")
    filt_mod.CustomFilter = object

    sys.modules["astrbot.api.message_components"] = mc
    sys.modules["astrbot.api.event"] = ev_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.event.filter"] = filt_mod


class _Event:
    def __init__(self, text="", umo="test:GroupMessage:100", platform="aiocqhttp"):
        self._text = text
        self.unified_msg_origin = umo
        self._platform = platform
        self.out: list = []
        self.tracked: list = []
        self.stopped = False

    # v1.6.8 起发送形态要按平台能力决策，事件必须能报自己是什么协议端。
    # 默认给 aiocqhttp（OneBot v11）—— 它能用合并转发，是本文件测的形态。
    def get_platform_name(self) -> str:
        return self._platform

    def get_platform_id(self) -> str:
        return "test_instance"

    def get_message_str(self) -> str:
        return self._text

    def get_sender_id(self) -> str:
        return "2593504303"

    def get_sender_name(self) -> str:
        return "管理员"

    def is_private_chat(self) -> bool:
        return False

    def plain_result(self, text):
        self.out.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.out.append(("chain", chain))
        return ("chain", chain)

    def stop_event(self) -> None:
        self.stopped = True

    def track_temporary_local_file(self, path) -> None:
        self.tracked.append(str(path))


async def _collect(agen):
    return [x async for x in agen]


def _strip_comments(seg: str) -> str:
    """逐行去掉注释内容，只留真实代码。

    注释里出现某个 API 名字**不算用了它** —— `_render_forward` 的说明里就写着
    「不能像直发那样直接用 ``Comp.Record.fromURL``」，按原文串检查会把这句
    注释误判成违规调用。
    """
    out: list[str] = []
    for line in seg.splitlines():
        quote: str | None = None
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
            elif ch == "#":
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _chain_item(out: list):
    """从结果列表里挑出那条 chain_result（转发消息）。"""
    for item in out:
        if item[0] == "chain":
            return item
    return None


def _nodes_of(head) -> list:
    """从 ``Comp.Nodes`` 里取节点列表。

    兼容两种环境：真实 AstrBot 是 ``Nodes.nodes``（pydantic 字段），本地 stub
    是位置参数 ``args[0]``。容器里跑同一个文件时走的是前者。
    """
    nodes = getattr(head, "nodes", None)
    if nodes is not None:
        return list(nodes)
    args = getattr(head, "args", None)
    if args and isinstance(args[0], (list, tuple)):
        return list(args[0])
    return []


def _content_of(node) -> list:
    """取一个 ``Node`` 里的组件列表（同为兼容两种环境）。"""
    content = getattr(node, "content", None)
    if content:
        return list(content)
    args = getattr(node, "args", None)
    if args and isinstance(args[0], (list, tuple)):
        return list(args[0])
    return []


def _text_of(comp) -> str:
    """取一个 ``Plain`` 的文本。"""
    text = getattr(comp, "text", None)
    if text is not None:
        return str(text)
    args = getattr(comp, "args", ())
    return str(args[0]) if args else ""


def _node_count(item) -> int:
    """从一条 ``chain_result`` 里取出合并转发的节点数。"""
    chain = item[1]
    if not chain:
        return -1
    head = chain[0]
    if type(head).__name__ != "Nodes":
        return -1
    return len(_nodes_of(head))


def _node_kinds(item) -> list[str]:
    """每个节点里第一个组件的类型名（用来验证顺序与媒体类型）。"""
    chain = item[1]
    head = chain[0]
    out = []
    for node in _nodes_of(head):
        comps = _content_of(node)
        out.append(type(comps[0]).__name__ if comps else "空")
    return out


def _first_node_text(item) -> str:
    """合并转发里第一个节点第一个组件的文本（用来验证简介内容）。"""
    chain = item[1]
    head = chain[0]
    nodes = _nodes_of(head)
    if not nodes:
        return ""
    comps = _content_of(nodes[0])
    return _text_of(comps[0]) if comps else ""


def behavior_checks() -> None:
    print()
    print("=" * 70)
    print("1. 开关与渲染路径")
    print("=" * 70)

    _install_astrbot_stubs()
    import astrbot_plugin_rconsole.main as main_mod
    from astrbot_plugin_rconsole.platforms.base import ResolveResult

    class FakePlugin(main_mod.Main):
        def __init__(self, conf=None):  # noqa: D107
            self._conf = conf or {}

        def conf_get(self, key, default=None):
            return self._conf.get(key, default)

    tmp = Path(tempfile.mkdtemp(prefix="rconsole_fwd_"))
    fake_video = tmp / "v.mp4"
    fake_video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"A" * 2048)

    # ---- 开关读取 ----
    check("默认关闭转发", FakePlugin()._forward_enabled(), False)
    check("配置为真时启用",
          FakePlugin({"plugin.send_as_forward": True})._forward_enabled(), True)

    # ---- 直发路径（开关关闭）----
    plugin = FakePlugin({"plugin.show_desc": True})
    result = ResolveResult.ok(
        "哔哩哔哩", title="标题", author="UP主", local_videos=[str(fake_video)]
    )
    out = asyncio.run(_collect(plugin._render(_Event(), result)))
    kinds = [k for k, _ in out]
    check_true("直发：产出多条消息（简介 + 媒体）", len(out) >= 2, str(kinds))
    check_true("直发：第一条是文字简介",
               out and out[0][0] == "plain" and "标题" in str(out[0][1]),
               str(out[0])[:60])
    check_true("直发：媒体是单独的 chain_result",
               any(k == "chain" for k, _ in out), str(kinds))
    check_true("直发：不是合并转发",
               all(_node_count(i) < 0 for i in out if i[0] == "chain"))

    # ---- 转发路径（开关打开）----
    plugin = FakePlugin({
        "plugin.show_desc": True,
        "plugin.send_as_forward": True,
    })
    out = asyncio.run(_collect(plugin._render(_Event(), result)))
    check("转发：只产出 1 条消息", len(out), 1)
    check_true("转发：那一条是 Nodes（合并转发）",
               out and out[0][0] == "chain" and _node_count(out[0]) >= 0,
               str(out[0])[:80])
    check("转发：简介节点 + 1 个视频节点", _node_count(out[0]), 2)
    check("转发：节点顺序 = 简介 → 视频", _node_kinds(out[0]), ["Plain", "Video"])
    check_true("转发：简介节点里有标题",
               "标题" in _first_node_text(out[0]),
               _first_node_text(out[0])[:80])
    check_true("转发：没有单独发「识别成功」提示",
               all(k != "plain" for k, _ in out))

    # ---- v1.6.8：官方机器人即使开了转发，也必须回落直发 ----
    # 官方（botpy）没有合并转发消息段，硬走会让整条消息链发送失败，
    # 用户看到的是「什么都没发出来」—— 所以能力表优先级高于配置开关。
    plugin = FakePlugin({
        "plugin.show_desc": True,
        "plugin.send_as_forward": True,
    })
    check("官机：_forward_enabled 被能力表短路为 False（关键）",
          plugin._forward_enabled(_Event(platform="qqofficial")), False)
    check("OneBot：同一份配置下仍然允许转发",
          plugin._forward_enabled(_Event()), True)
    check("未知协议端：保守回落，不发转发",
          plugin._forward_enabled(_Event(platform="some_new_platform")), False)

    out = asyncio.run(
        _collect(plugin._render(_Event(platform="qqofficial"), result))
    )
    check_true("官机：产出多条消息（没有塌成一条合并转发）",
               len(out) >= 2, str([k for k, _ in out]))
    check_true("官机：没有任何合并转发消息",
               all(_node_count(i) < 0 for i in out if i[0] == "chain"),
               str([k for k, _ in out]))
    check_true("官机：媒体仍然是单独一条 chain_result",
               any(k == "chain" for k, _ in out))

    # ---- show_desc 关闭 ----
    plugin = FakePlugin({
        "plugin.show_desc": False,
        "plugin.send_as_forward": True,
    })
    out = asyncio.run(_collect(plugin._render(_Event(), result)))
    check("关闭简介后只有 1 个媒体节点", _node_count(out[0]), 1)
    check("关闭简介后节点是视频", _node_kinds(out[0]), ["Video"])

    # ---- 无媒体：不硬发空壳聊天记录 ----
    plugin = FakePlugin({"plugin.send_as_forward": True})
    empty = ResolveResult.ok("微博", title="只有标题")
    out = asyncio.run(_collect(plugin._render_forward(
        _Event(), empty, "🔗 识别：", True)))
    check("无媒体时退回普通文本", len(out), 1)
    check("退回的是 plain（不是转发）", out[0][0], "plain")
    check_true("文字里带上已知情报", "只有标题" in str(out[0][1]), str(out[0][1])[:60])

    # ---- 视频直链模式 ----
    plugin = FakePlugin({
        "plugin.send_as_forward": True,
        "plugin.send_mode": "url",
    })
    linked = ResolveResult.ok("抖音", title="t", videos=["https://example.com/a.mp4"])
    out = asyncio.run(_collect(plugin._render_forward(
        _Event(), linked, "🔗 识别：", True)))
    check("直链模式：视频进节点", _node_kinds(out[0]), ["Plain", "Video"])
    check_true("直链模式：没有单独报错", all(k != "plain" for k, _ in out),
               str(out))

    # ---- 音频：下载失败不能拖垮整条转发 ----
    #
    # 合并转发里的 Record 会被 AstrBot 下载并转 wav（Node.to_dict →
    # Record.convert_to_base64），所以必须先落盘；这里用一个必定连不上的地址
    # 验证「失败只是少一个节点 + 一条提示」，而不是整条聊天记录发不出去。
    plugin = FakePlugin({"plugin.send_as_forward": True})
    mixed = ResolveResult.ok(
        "网易云音乐", title="歌",
        local_videos=[str(fake_video)],
        audios=["http://127.0.0.1:1/none.mp3"],
    )
    out = asyncio.run(_collect(plugin._render_forward(
        _Event(), mixed, "🔗 识别：", True)))
    check("音频失败时视频照发（节点 = 简介 + 视频）",
          _node_kinds(_chain_item(out)), ["Plain", "Video"])
    check_true("音频失败会给出提示", any(k == "plain" for k, _ in out), str(out))
    check_true("提示里说明是音频",
               "音频" in str(out[0][1]) if out and out[0][0] == "plain" else False,
               str(out[0][1])[:80] if out else "")

    # 只有音频且失败 → 没有媒体节点，退回文本情报（不硬发空壳聊天记录）
    plugin = FakePlugin({"plugin.send_as_forward": True})
    only_audio = ResolveResult.ok("网易云音乐", title="歌",
                                  audios=["http://127.0.0.1:1/none.mp3"])
    out = asyncio.run(_collect(plugin._render_forward(
        _Event(), only_audio, "🔗 识别：", True)))
    check("只有音频且失败 → 退回文本", out[0][0], "plain")
    check_true("没有发出空壳聊天记录", _chain_item(out) is None, str(out))

    # ---- 纯文本结果（AI 总结）永远直发 ----
    plugin = FakePlugin({"plugin.send_as_forward": True})
    text_only = ResolveResult.ok("AI总结", desc="这是一段总结")
    text_only.extra["text_only"] = True
    out = asyncio.run(_collect(plugin._render(_Event(), text_only)))
    check("纯文本结果直发", out[0][0], "plain")
    check_true("纯文本结果内容是总结", "这是一段总结" in str(out[0][1]))

    # ---- 本地文件登记（防泄漏）----
    ev = _Event()
    asyncio.run(_collect(plugin._render_forward(ev, result, "🔗 识别：", True)))
    check_true("本地媒体登记给 AstrBot 回收", str(fake_video) in ev.tracked,
               str(ev.tracked))


def source_checks() -> None:
    print()
    print("=" * 70)
    print("2. 源码不变量")
    print("=" * 70)

    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _fn(name: str):
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name == name:
                return node
        return None

    fwd = _fn("_render_forward")
    check_true("_render_forward 存在", fwd is not None)

    if fwd is not None:
        body = ast.get_source_segment(src, fwd) or ""
        code = _strip_comments(body)
        check_true("转发路径不用 Comp.Video.fromFileSystem（跨容器必 ENOENT）",
                   "Comp.Video.fromFileSystem" not in code)
        # Record 在合并转发里会被 AstrBot 下载+转 wav，失败会拖垮整条聊天记录
        check_true("转发路径的音频先落盘（不用 Comp.Record.fromURL）",
                   "Comp.Record.fromURL" not in code)
        check_true("转发路径落了音频本地文件", "Comp.Record.fromFileSystem" in code)

        # 每个 _video_component 调用点都要 await（漏了会静默拿到 coroutine）
        lines = src.splitlines()
        bad = [
            f"line {n.lineno}: {lines[n.lineno - 1].strip()[:60]}"
            for n in ast.walk(fwd)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_video_component"
            and "await self._video_component" not in lines[n.lineno - 1]
        ]
        check("转发路径里 _video_component 调用都带 await", bad, [])
        check_true("转发路径确实调用了 _video_component",
                   "await self._video_component" in body)

    # _render 必须是「分派」而不是把两条路径揉在一起
    render = _fn("_render")
    render_body = ast.get_source_segment(src, render) if render else ""
    check_true("_render 里按 _forward_enabled() 分派",
               "_render_body" in render_body)
    body_fn = _fn("_render_body")
    body_src = ast.get_source_segment(src, body_fn) if body_fn else ""
    check_true("_render_body 里判断了 _forward_enabled(带 event)",
               "_forward_enabled(event)" in body_src)
    check_true("_render_body 会调用 _render_forward", "_render_forward" in body_src)
    check_true("_render_body 会调用 _render_direct", "_render_direct" in body_src)

    # v1.6.8：合并转发要受「平台能力」管 —— QQ 官方机器人没有合并转发消息段，
    # 配置开着也不能走，否则整条消息链发送失败（用户看到「什么都没发出来」）
    fwd = _fn("_forward_enabled")
    fwd_src = ast.get_source_segment(src, fwd) if fwd else ""
    check_true("_forward_enabled 会查平台能力", "_caps(event)" in fwd_src)
    check_true("_forward_enabled 对官方机器人短路", "not self._caps(event).forward" in fwd_src)


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 聊天记录转发回归测试")
    print("=" * 70)
    behavior_checks()
    source_checks()

    print()
    print("=" * 70)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：")
        for name in _FAILED:
            print(f"   · {name}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
