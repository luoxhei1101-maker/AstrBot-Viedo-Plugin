"""序号点播「一次性」语义的离线回归测试。

**背景**：用户反馈「已经发了序号拿到音乐，再发一次同一个序号会重复再发一遍」。
根因是 ``_take_music_pick()`` **只读不消费** —— 会话一直留到 TTL 到期，
于是同一个序号在 60 秒内可以无限重发。

要守住的不变量：

    **点播成功后立刻消费会话；再发同一个序号应当静默无响应。
    序号越界不消费（让用户能重试）。**

另外锁一个并发安全点：消费必须发生在 ``cmd_music_pick`` 的**第一个 await 之前**
（asyncio 单线程，那段没有让出点，所以连点两次不会各播一遍）。

跑法::

    python tests/test_music_pick.py
"""

from __future__ import annotations

import ast
import asyncio
import sys
import time
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 用包名导入（插件内部有 `from ..core.x import`，必须作为包）
sys.path.insert(0, str(_ROOT.parent))


def _real_astrbot_available() -> bool:
    """容器里 AstrBot 是真装着的，那种情况**不要**打桩。

    这样同一个测试文件既能在本机跑（自动桩出最小 API），
    也能直接丢进容器跑一遍 —— 后者验证的是**已部署的那份代码**。
    """
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
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       期望: {want}")
        print(f"       实际: {got}")
        _FAILED.append(name)


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


# ======================================================================
# 1. 静态检查：消费必须发生在第一个 await 之前
# ======================================================================
def static_checks() -> None:
    print("=" * 70)
    print("1. 静态检查：清理会话在第一个 await 之前")
    print("=" * 70)

    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "cmd_music_pick"
        ):
            fn = node
            break
    check_true("找到 cmd_music_pick", fn is not None)
    if fn is None:
        return

    forget_lines = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_forget_music_pick"
    ]
    check_true("cmd_music_pick 里调用了 _forget_music_pick", forget_lines)

    await_lines = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Await)]
    check_true("cmd_music_pick 里存在 await（用于比较先后）", await_lines)

    if forget_lines and await_lines:
        check_true(
            "消费发生在第一个 await 之前（并发连点安全）",
            min(forget_lines) < min(await_lines),
        )

    # _take_music_pick 自己不消费（越界时要能重试）
    take = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_take_music_pick":
            take = node
            break
    check_true("找到 _take_music_pick", take is not None)
    if take is not None:
        pops = [
            n
            for n in ast.walk(take)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "pop"
        ]
        # 只允许「过期时」那一次 pop，不能无条件删除
        check("_take_music_pick 里的 pop 只有 1 处（仅过期清理）", len(pops), 1)
        snippet = ast.get_source_segment(src, take) or ""
        check_true(
            "_take_music_pick 的 pop 只在 TTL 判断里（有条件删除）",
            "_MUSIC_PICK_TTL" in snippet,
        )


def _install_astrbot_stubs() -> None:
    """按需桩出 main.py 依赖的最小 AstrBot API 集合。

    ``main`` 顶层会 import 消息组件 / 事件 / Star 基类，本地没有这些包，
    所以先把模块塞进 ``sys.modules``。被测逻辑只用得到
    ``plain_result`` / ``chain_result``，组件能收构造参数就够了。
    """
    if not NEED_STUBS:
        print("  (真实 AstrBot 环境，跳过打桩)")
        return

    mc = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, **kw):
            self.kw = kw
            for k, v in kw.items():
                setattr(self, k, v)

    for name in ("Image", "Video", "Record", "Plain", "Node", "Nodes",
                 "Json", "Music", "File"):
        setattr(mc, name, type(name, (_Comp,), {}))
    mc.MessageChain = _Comp

    class _AnyFilter:
        """任何 filter 属性都返回「接受任意参数、返回恒等装饰器」的可调用对象。

        注意**不能**做「单个可调用参数就直通」的优化：``@filter.custom_filter(SomeFilter)``
        传进来的就是类本身，直通会把它当装饰器调用而炸掉。
        """

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


# ======================================================================
# 2. 行为检查：真的跑 cmd_music_pick
# ======================================================================
class _Song:
    def __init__(self, name: str, artist: str = "歌手"):
        self.name = name
        self.artist = artist
        self.album = "专辑"
        self.cover = ""
        self.page_url = f"https://example.com/{name}"
        self.duration = 100
        self.platform = "netease"
        self.play_url = ""
        self.song_id = "1"
        self.extra: dict = {}

    @property
    def label(self) -> str:
        return f"{self.name} - {self.artist}"


class _Event:
    def __init__(self, text: str, umo: str = "test:GroupMessage:100"):
        self._text = text
        self.unified_msg_origin = umo
        self.out: list = []

    def get_message_str(self) -> str:
        return self._text

    def plain_result(self, text):
        self.out.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.out.append(("chain", chain))
        return ("chain", chain)


async def _collect(agen):
    return [x async for x in agen]


def behavior_checks() -> None:
    print()
    print("=" * 70)
    print("2. 行为检查：跑真实的 cmd_music_pick")
    print("=" * 70)

    _install_astrbot_stubs()
    import astrbot_plugin_rconsole.main as main_mod
    class FakePlugin(main_mod.Main):
        def __init__(self):  # noqa: D107 - 故意不调 super().__init__
            self._music_sessions = {}

        def conf_get(self, key, default=None):
            return {
                "music.enable": True,
                "music.searchMode": "list",
                "music.sendMode": "link",
            }.get(key, default)

        def cookie_for(self, _platform):
            return ""

    plugin = FakePlugin()
    umo = "test:GroupMessage:100"
    songs = [_Song("晴天"), _Song("稻香")]

    def remember(ts: float | None = None) -> None:
        plugin._music_sessions[umo] = (
            ts if ts is not None else time.time(),
            "晴天",
            "网易云音乐",
            "netease",
            list(songs),
        )

    def send(text: str) -> list:
        ev = _Event(text)
        return asyncio.run(_collect(plugin.cmd_music_pick(ev))), ev

    # ---- 首次点播：应该播第 1 首，并且消费掉会话 ----
    remember()
    out, ev = send("1")
    check("首次回 1 有响应", len(out), 1)
    check_true(
        "响应里是第 1 首（晴天）",
        out and "晴天" in str(out[0]),
    )
    check("点播后会话被清掉", umo in plugin._music_sessions, False)

    # ---- 关键回归：再发一次同一个序号，必须静默无响应 ----
    out2, ev2 = send("1")
    check("再发一次 1 无响应（这是本次修的 bug）", out2, [])
    check("且没有产生任何消息", len(ev2.out), 0)

    # ---- 换一个序号同样无响应 ----
    out3, _ = send("2")
    check("会话清掉后发 2 也无响应", out3, [])

    # ---- 序号越界：给提示但**不**消费会话，可继续重试 ----
    remember()
    out4, _ = send("9")
    check("越界序号有提示", len(out4), 1)
    check_true("提示里说明范围", "1-2" in str(out4[0]))
    check("越界后会话仍在（可重试）", umo in plugin._music_sessions, True)
    out5, _ = send("2")
    check("越界后改发 2 正常播放", len(out5), 1)
    check_true("播的是第 2 首（稻香）", out5 and "稻香" in str(out5[0]))
    check("成功后被清掉", umo in plugin._music_sessions, False)

    # ---- 0 也算越界 ----
    remember()
    out6, _ = send("0")
    check("序号 0 给提示", len(out6), 1)
    check("序号 0 不消费会话", umo in plugin._music_sessions, True)

    # ---- 过期会话：无响应，且顺手清理 ----
    remember(time.time() - 999)
    out7, _ = send("1")
    check("过期会话无响应", out7, [])
    check("过期会话被清理", umo in plugin._music_sessions, False)

    # ---- 没有会话时，普通数字消息不受影响 ----
    out8, _ = send("123")
    check("无会话时数字消息不被拦截", out8, [])

    # ---- 非数字文本直接忽略 ----
    remember()
    out9, _ = send("你好")
    check("非数字输入忽略", out9, [])
    check("非数字不消费会话", umo in plugin._music_sessions, True)

    # ---- 不同会话互不干扰 ----
    plugin._music_sessions[umo] = (
        time.time(), "晴天", "网易云音乐", "netease", list(songs)
    )
    other = _Event("1", umo="test:GroupMessage:200")
    out10 = asyncio.run(_collect(plugin.cmd_music_pick(other)))
    check("别的会话没有会话记录，无响应", out10, [])
    check("原会话未被影响", umo in plugin._music_sessions, True)

    # ---- 功能开关关闭时不响应 ----
    class Disabled(FakePlugin):
        def conf_get(self, key, default=None):
            if key == "music.enable":
                return False
            return super().conf_get(key, default)

    d = Disabled()
    d._music_sessions[umo] = (
        time.time(), "晴天", "网易云音乐", "netease", list(songs)
    )
    out11 = asyncio.run(_collect(d.cmd_music_pick(_Event("1"))))
    check("关闭点歌开关后不响应", out11, [])

    # ---- searchMode=direct 时不响应序号 ----
    class Direct(FakePlugin):
        def conf_get(self, key, default=None):
            if key == "music.searchMode":
                return "direct"
            return super().conf_get(key, default)

    dd = Direct()
    dd._music_sessions[umo] = (
        time.time(), "晴天", "网易云音乐", "netease", list(songs)
    )
    out12 = asyncio.run(_collect(dd.cmd_music_pick(_Event("1"))))
    check("searchMode=direct 时不响应序号", out12, [])


def main() -> int:
    static_checks()
    behavior_checks()
    print()
    print("=" * 70)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：")
        for n in _FAILED:
            print("   -", n)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
