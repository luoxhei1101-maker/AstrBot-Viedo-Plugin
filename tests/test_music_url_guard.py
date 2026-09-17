# -*- coding: utf-8 -*-
"""直链可用性守卫的离线测试。

守两件事：

1. **``netease_play_url`` 不许再回落到 ``song/media/outer/url``**
   —— 那个接口已废弃（实测恒 302 到 ``music.163.com/404`` 返回 HTML），
   回落的结果是用户收到一个内容是 404 网页的「xxx.mp3」。
2. **``verify_audio_url`` 的判定正确**：能拦坏链接、不误杀好链接、
   网络异常时放行、并带缓存。

环境自适应：本机桩出最小的 ``astrbot.api``，容器里用真实的。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))

if "astrbot" not in sys.modules:
    _api = types.ModuleType("astrbot.api")
    _api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api.AstrBotConfig = dict
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

import astrbot_plugin_rconsole.core.http as http_mod  # noqa: E402
from astrbot_plugin_rconsole.core import music_search as ms  # noqa: E402
from astrbot_plugin_rconsole.core.constants import (  # noqa: E402
    COMMAND_RULES,  # noqa: F401 - 顺带确认常量模块可导入
)

PASS = 0
FAIL = 0


def check(name: str, got, want=True) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        print(f"  [!! ] {name}: got={got!r} want={want!r}")


# ==========================================================================
# 假 HTTP 层
# ==========================================================================


class FakeResp:
    def __init__(self, status=200, ctype="audio/mpeg", url="http://cdn/x.mp3"):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """记录调用次数，便于验证缓存。"""

    def __init__(self, resp: FakeResp | Exception):
        self.resp = resp
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def install_session(resp) -> FakeSession:
    sess = FakeSession(resp)
    http_mod.get_session = lambda: sess  # type: ignore[assignment]
    return sess


# ==========================================================================
# 静态断言
# ==========================================================================


def static_checks() -> None:
    print("\n--- 静态：不再回落到已废弃的接口 ---")
    src = (_ROOT / "core" / "music_search.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "netease_play_url"), None)
    check("找到 netease_play_url", fn is not None)
    if fn is None:
        return

    body = ast.get_source_segment(src, fn) or ""
    check("函数体内不引用 NETEASE_SONG_OUTER_URL",
          "NETEASE_SONG_OUTER_URL" not in body)
    # 只看「非 docstring 的字符串常量」—— docstring 里会解释「为什么不要回落」
    doc_value = None
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        doc_value = fn.body[0].value
    literals = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n is not doc_value]
    check("函数体内无 outer/url 字符串常量（docstring 除外）",
          [s for s in literals if "outer/url" in s], [])
    check("函数体末尾返回空串", 'return ""' in body)

    # 常量本身应已被注释掉
    const_src = (_ROOT / "core" / "constants.py").read_text(encoding="utf-8")
    for line in const_src.splitlines():
        if "NETEASE_SONG_OUTER_URL =" in line:
            check(f"常量行已被注释: {line.strip()[:50]}", line.lstrip().startswith("#"))
    live = [l for l in const_src.splitlines()
            if l.startswith("NETEASE_SONG_OUTER_URL")]
    check("没有生效的 NETEASE_SONG_OUTER_URL 常量", live, [])


# ==========================================================================
# 行为断言
# ==========================================================================


async def behavior_checks() -> None:
    print("\n--- 行为：verify_audio_url ---")

    check("空串直接 False（不发请求）", await ms.verify_audio_url(""), False)

    ms._AUDIO_OK_CACHE.clear()
    install_session(FakeResp(200, "audio/mpeg"))
    check("audio/mpeg -> True", await ms.verify_audio_url("http://cdn/a.mp3"), True)

    ms._AUDIO_OK_CACHE.clear()
    sess = install_session(FakeResp(200, "audio/mpeg"))
    await ms.verify_audio_url("http://cdn/cache.mp3")
    await ms.verify_audio_url("http://cdn/cache.mp3")
    await ms.verify_audio_url("http://cdn/cache.mp3")
    check("同一 URL 只校验一次（缓存生效）", sess.calls, 1)

    ms._AUDIO_OK_CACHE.clear()
    install_session(FakeResp(200, "text/html;charset=utf8",
                             "http://music.163.com/song/media/outer/url?id=1"))
    check("text/html -> False（就是那个废弃接口）",
          await ms.verify_audio_url("http://x/outer"), False)

    ms._AUDIO_OK_CACHE.clear()
    install_session(FakeResp(200, "application/json"))
    check("application/json -> False", await ms.verify_audio_url("http://x/a.json"), False)

    ms._AUDIO_OK_CACHE.clear()
    install_session(FakeResp(404, "text/html"))
    check("HTTP 404 -> False", await ms.verify_audio_url("http://cdn/miss.mp3"), False)

    ms._AUDIO_OK_CACHE.clear()
    install_session(FakeResp(200, "audio/mpeg", "http://music.163.com/404"))
    check("被重定向到 /404 -> False",
          await ms.verify_audio_url("http://cdn/redir.mp3"), False)

    ms._AUDIO_OK_CACHE.clear()
    install_session(OSError("network down"))
    check("网络异常 -> True（宁可放过，不误杀）",
          await ms.verify_audio_url("http://cdn/flaky.mp3"), True)

    ms._AUDIO_OK_CACHE.clear()
    install_session(FakeResp(200, "audio/mpeg"))
    check("audio/mp4 也算音频", await ms.verify_audio_url("http://cdn/a.m4a"), True)


async def no_fallback_check() -> None:
    """取不到直链时必须返回空串，而不是拼一个 outer/url 出来。"""
    print("\n--- 行为：netease_play_url 取不到就返回空 ---")

    class BoomResp:
        async def __aenter__(self):
            raise OSError("boom")

        async def __aexit__(self, *exc):
            return False

    class BoomSession:
        def post(self, *a, **k):
            return BoomResp()

    http_mod.get_session = lambda: BoomSession()  # type: ignore[assignment]

    song = ms.Song(
        platform="netease", song_id="186016", name="晴天", artist="周杰伦",
        page_url="http://music.163.com/#/song?id=186016", extra={},
    )
    url = await ms.netease_play_url(song, "")
    check("请求异常时返回空串（不拼 outer/url）", url, "")
    check("返回值里不含 outer/url", "outer/url" not in url)


def main() -> int:
    static_checks()
    asyncio.run(behavior_checks())
    asyncio.run(no_fallback_check())

    print()
    print("=" * 62)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("有失败项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
