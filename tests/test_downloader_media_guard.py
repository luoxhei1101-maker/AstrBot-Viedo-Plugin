"""下载器的「软失败」防护测试（v1.6.2）。

**要防的具体问题**：抖音签名 CDN 拿不到图时**不返回 4xx/5xx**，而是回一个
``200`` + ``Content-Type: text/html`` 的 **238 字节错误页**（实测）。

	v1.6.1 的 ``_stream_one`` 只检查 ``resp.status != 200``，所以：
	- 这个 238 字节的 HTML 会被**当成图片写盘**
	- 然后 ``Comp.Image.fromFileSystem`` 把它发给 QQ

用户看到的就是一张破图；而如果有候选是这种「假 200」，候选回退逻辑
**不会继续往下试**，反而把真正能下的原图候选跳过了。

所以 ``_stream_one(expect_media=True)`` 现在做两道校验：

1. ``Content-Type`` 必须是 image/ video/ audio/ 或 octet-stream
2. 内容不能小于 1KB（正常图片/视频远大于此）

跑法::

    python tests/test_downloader_media_guard.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

if "astrbot" not in sys.modules:
    _logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api = types.ModuleType("astrbot.api")
    _api.logger = _logger
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

from core.downloader import (  # noqa: E402
    _stream_one,
    download_many_candidates,
)

_FAILED: list[str] = []


def check_true(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        if detail:
            print(f"       {detail}")
        _FAILED.append(name)


# 真实抓到的 403 错误页特征：238 字节 HTML
ERROR_PAGE = b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n" + b"x" * 180
REAL_IMAGE = b"\xff\xd8\xff\xe0" + b"I" * 200_000  # 200KB 假 JPEG


class FakeContent:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]

    async def read(self, n: int = -1):
        return self._body if n < 0 else self._body[:n]


class FakeResp:
    def __init__(self, body: bytes, ctype: str, status: int = 200, clen=None):
        self._body = body
        self.status = status
        self.content_type = ctype
        self.content_length = clen if clen is not None else len(body)
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """按 URL 后缀决定返回什么，用来模拟「坏候选 -> 好候选」的回退。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.seen: list[str] = []

    def get(self, url, **kw):
        self.seen.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return FakeResp(REAL_IMAGE, "image/jpeg")


def _run(coro):
    return asyncio.run(coro)


def main() -> int:
    # ---- 1. 错误页必须被拦掉 ----
    async def t1():
        s = FakeSession({"/bad": FakeResp(ERROR_PAGE, "text/html", 200, 238)})
        try:
            await _stream_one(
                s, "https://p3-pc-sign.douyinpic.com/bad", prefix="t", max_bytes=0,
                expect_media=True,
            )
            return None, None
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__, str(exc)

    err, msg = _run(t1())
    check_true(
        "200 + text/html 错误页被拦掉（不写盘、不返路径）",
        err is not None,
        f"err={err} msg={msg}",
    )
    check_true(
        "报错信息指明是内容类型问题",
        "非媒体内容" in (msg or ""),
        f"实际: {msg}",
    )

    # ---- 2. 没开 expect_media 时行为不变（向后兼容）----
    async def t2():
        s = FakeSession({"/bad": FakeResp(ERROR_PAGE, "text/html", 200, 238)})
        return await _stream_one(
            s, "https://x.com/bad", prefix="t2", max_bytes=0, expect_media=False
        )

    p = _run(t2())
    check_true("不开 expect_media 时错误页仍会落盘（旧行为不变）", p is not None)
    if p:
        p.unlink(missing_ok=True)

    # ---- 3. 真实图片正常通过 ----
    async def t3():
        s = FakeSession({"/ok": FakeResp(REAL_IMAGE, "image/jpeg")})
        return await _stream_one(
            s, "https://p3-sign.douyinpic.com/ok", prefix="t3", max_bytes=0,
            expect_media=True,
        )

    p3 = _run(t3())
    check_true("真实图片（200KB jpeg）正常下载通过", p3 is not None)
    if p3:
        check_true("落盘大小正确", p3.stat().st_size == len(REAL_IMAGE))
        p3.unlink(missing_ok=True)

    # ---- 4. 过小的「成功」响应也要拦（没有 Content-Type 误导时）----
    async def t4():
        s = FakeSession({"/tiny": FakeResp(b"\xff\xd8\xff" + b"x" * 100, "image/jpeg")})
        try:
            await _stream_one(
                s, "https://x.com/tiny", prefix="t4", max_bytes=0, expect_media=True
            )
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    msg4 = _run(t4())
    check_true(
        "过小响应（<1KB）也被拦掉",
        msg4 is not None and "过小" in msg4,
        f"实际: {msg4}",
    )

    # ---- 5. 候选回退：坏候选被识别后继续试好候选 ----
    # download_many_candidates 自己建 session，没法注入假 session，
    # 所以替换 _stream_one 来验证它的回退语义：
    # 第一个候选抛错 -> 继续试第二个；第一个 None、第二个有路径。
    async def t5b():
        import aiohttp  # noqa: F401

        import core.downloader as dl

        calls: list[str] = []

        async def fake_stream(session, url, *, prefix, max_bytes, expect_media=False):
            calls.append(url)
            if "cand-bad" in url:
                raise Exception("非媒体内容")
            pp = _ROOT / "tests" / "_tmp_guard_good.jpg"
            pp.write_bytes(REAL_IMAGE)
            return pp

        orig = dl._stream_one
        dl._stream_one = fake_stream
        try:
            res = await dl.download_many_candidates(
                [["https://x.com/cand-bad"], ["https://x.com/cand-good"]],
                prefix="t5b",
            )
            return res, calls
        finally:
            dl._stream_one = orig

    res, calls = _run(t5b())
    check_true("坏候选 -> None（不抛异常）", res[0] is None, f"实际 {res[0]}")
    check_true("好候选 -> 有本地路径", res[1] is not None)
    check_true(
        "候选回退确实试到了第二个 URL",
        any("cand-good" in u for u in calls),
        f"calls={calls}",
    )
    for r in res:
        if r:
            Path(r).unlink(missing_ok=True)

    print("\n" + "=" * 52)
    if _FAILED:
        print(f"{len(_FAILED)} 项失败：{', '.join(_FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
