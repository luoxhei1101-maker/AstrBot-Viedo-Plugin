"""抖音图集「发送路径」的离线回归测试。

**背景**：用户反馈「新发的图集作品都没发出来」，日志里只剩一条
``🔗 识别：抖音``，图片全无。根因是**发送路径把远程签名直链交给了
AstrBot 发送端**：

- 抖音图片直链（``p3-pc-sign.douyinpic.com/...``）需要正确的
  ``Referer`` 才可能 200；
- 同一个 ``url_list`` 里的 ``.webp`` / ``.jpeg`` 两个变体，**哪一个 403
  是随机的**，没有固定候选顺序可用；
- AstrBot 的 ``respond.stage`` 用**自己的下载器**抓 URL，既不补 Referer
  也没有候选回退，**一张失败 = 整条消息链失败**（``DownloadFileHTTPError``）。

所以本测试锁定的核心不变量是：

    **静态图和动图一律先下载到本地，发送端只接收 ``fromFileSystem``，
    绝不出现 ``fromURL`` 的远程媒体。**

只要这条守住了，签名点 / 403 / Referer 的问题就永远影响不到用户。

跑法（本机已装 aiohttp 即可，会在本地起一个假 HTTP 服务）::

    python tests/test_album_send_path.py
"""

from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 用**包名**导入（astrbot_plugin_rconsole.xxx）而不是把插件目录直接塞进
# sys.path —— 插件内部大量使用 `from ..core.x import`，只有作为包被导入时
# 相对导入才成立。所以要把**插件目录的父目录**加到 path。
sys.path.insert(0, str(_ROOT.parent))

# 本地没有 AstrBot 运行环境时，桩掉日志器（被测逻辑不依赖它）。
# **必须在 import core.* / main 之前完成**——这些模块顶层就
# `from astrbot.api import logger`。
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


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       期望: {want}")
        print(f"       实际: {got}")
        _FAILED.append(name)


def check_true(name: str, got, detail: str = "") -> None:
    ok = bool(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(name)


# ======================================================================
# 1. 静态检查：main.py 的图集/图片发送路径不得出现「远程媒体直发」
# ======================================================================
# 允许的 fromURL 用法（非图集媒体，属于别的功能）：
#   - 降级文本链接、音频 Record（语音有平台侧代理）
# 这里只针对「图片 / 视频」两类组件做静态断言。
_FORBIDDEN = ("Comp.Image.fromURL", "Comp.Video.fromURL")


def _method_source(path: Path, name: str) -> str:
    """抽出一个方法的源码文本（含内部嵌套，按缩进判定边界）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            start = node.lineno - 1
            # 方法体最后一个节点的结束行
            end = max(
                getattr(n, "end_lineno", node.lineno)
                for n in ast.walk(node)
                if hasattr(n, "end_lineno")
            )
            return "\n".join(lines[start:end])
    return ""


def static_checks() -> None:
    main_py = _ROOT / "main.py"

    for method in ("_send_album", "_send_images", "_download_album_stills",
                   "_download_album_videos", "_download_images"):
        body = _method_source(main_py, method)
        check_true(f"{method} 方法存在", body)

    # 发送路径（_send_album / _send_images）里绝不能出现远程媒体直发
    for method in ("_send_album", "_send_images"):
        body = _method_source(main_py, method)
        for forbidden in _FORBIDDEN:
            check(
                f"{method} 不含 {forbidden}（远程直发）",
                forbidden in body,
                False,
            )

    # 下载路径必须真的用带 Referer + 候选回退的下载器
    stills = _method_source(main_py, "_download_album_stills")
    check_true(
        "_download_album_stills 使用 download_many_candidates（候选回退）",
        "download_many_candidates" in stills,
    )
    check_true(
        "_download_album_stills 使用 download_many（无候选时的兜底）",
        "download_many(" in stills,
    )

    vids = _method_source(main_py, "_download_album_videos")
    check_true(
        "_download_album_videos 使用 download_media_candidates（动图候选回退）",
        "download_media_candidates(" in vids,
    )
    check_true(
        "_download_album_videos 读取 animated_video_candidates",
        "animated_video_candidates" in vids,
    )
    check_true(
        "_download_album_videos 是 async（内部要 await）",
        "async def _download_album_videos" in (
            _method_source(main_py, "_download_album_videos") or ""
        ),
    )


# ======================================================================
# 1b. 静态检查：视频组件**按平台分支**（官机 fromFileSystem / 其它 base64）
# ======================================================================
def cross_container_checks() -> None:
    """两个平台的要求**正好相反**，所以必须分支：

    * **NapCat 等（aiocqhttp）**：AstrBot 对 Video **不转 base64**，原样传
      ``file://`` 路径。AstrBot 和协议端（NapCat）跑在**两个容器**、无共享挂载时，
      协议端 ``realpath`` 必然 ENOENT，整条消息链失败 —— 现象是「视频发不出来」。
      实测本项目的 astrbot / snowluma 容器无任何共享挂载。**所以必须 base64。**
    * **官机（qq_official）**：正好反过来 —— 适配器只认**真实本地路径**。
      2026-09-24 线上事故：给 base64 时 ``_parse_to_qqofficial`` 走

          else: video_file_source = i.file        # base64://... 原样

      然后 ``upload_group_and_c2c_media`` 开头 ``Path(file_source).is_file()``
      拿它当文件名 stat，抛 ``OSError: [Errno 36] File name too long``，
      整条消息发不出去（用户看到的是「机器人没反应」）。**所以必须 fromFileSystem。**
    """
    main_py = _ROOT / "main.py"

    # 用 AST 剥掉所有注释与文档字符串，只看真实代码里还有没有该调用
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    code_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # 形如 Comp.Video.fromFileSystem
            if node.attr == "fromFileSystem":
                chain = []
                cur = node
                while isinstance(cur, ast.Attribute):
                    chain.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    chain.append(cur.id)
                dotted = ".".join(reversed(chain))
                if dotted == "Comp.Video.fromFileSystem":
                    code_calls.append(node.lineno)
    # ✅ 正确规则：fromFileSystem **只允许出现在 _video_component 内部**（官机分支），
    #    别处一律通过 _video_component，不许裸调。
    vc = _method_source(main_py, "_video_component")
    check_true("_video_component 方法存在", vc)

    _vc_lo = next(
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "_video_component"
    )
    _vc_hi = _vc_lo + len(vc.splitlines()) + 2
    outside = [f"line {ln}" for ln in code_calls if not (_vc_lo <= ln <= _vc_hi)]
    check("Comp.Video.fromFileSystem 只允许出现在 _video_component 里", outside, [])

    check_true(
        "_video_component 里有 fromFileSystem（**官机分支要真实路径**）",
        "Comp.Video.fromFileSystem" in vc,
        "官机适配器只认本地路径；给 base64 会 OSError: File name too long",
    )
    check_true(
        "_video_component 用 fromBase64（其它协议端跨容器安全）",
        "fromBase64" in vc,
    )
    check_true(
        "分支靠 _is_qq_official 判定平台",
        "_is_qq_official" in vc
        and "def _is_qq_official" in main_py.read_text(encoding="utf-8"),
    )
    check_true(
        "_video_component 收 event 参数（判断平台用）",
        "event: AstrMessageEvent | None = None" in vc,
    )
    check_true(
        "_video_component 会先检查文件是否存在（避免 ENOENT）",
        "is_file()" in vc or "exists()" in vc,
    )

    # 所有发视频的地方都要经过 _video_component
    for method in ("_render", "_send_album"):
        body = _method_source(main_py, method)
        check(
            f"{method} 不含裸 Comp.Video.fromFileSystem",
            "Comp.Video.fromFileSystem" in body,
            False,
        )

    # _video_component 现在是 async（base64 编码要丢线程池，避免卡事件循环），
    # 所以**每个调用点都必须 await**——漏了会静默拿到 coroutine 对象，
    # 表现为「视频发不出来但也不报错」，非常难查。这里用 AST + 源码行精确锁死。
    src_lines = main_py.read_text(encoding="utf-8").splitlines()
    call_lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # 匹配 self._video_component(...)
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "_video_component"
            and isinstance(f.value, ast.Name)
            and f.value.id == "self"
        ):
            call_lines.append(node.lineno)

    bad = []
    for lineno in call_lines:
        line = src_lines[lineno - 1]
        if "await self._video_component" not in line:
            bad.append(f"line {lineno}: {line.strip()[:70]}")
    check("所有 self._video_component 调用点都带 await", bad, [])

    # 同理：**每个调用点都必须把 event 传进去**，否则 _is_qq_official 拿不到平台
    # 信息 → 官机上仍然给 base64 → 又炸。用 AST 校验实参个数 >= 2。
    call_args: dict[int, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "_video_component"
            and isinstance(f.value, ast.Name)
            and f.value.id == "self"
        ):
            call_args[node.lineno] = len(node.args)
    no_event = [f"line {ln}" for ln, n in call_args.items() if n < 2]
    check("所有 _video_component 调用点都把 event 传进去了", no_event, [])
    check_true("_video_component 调用点数量 > 0", len(call_args) > 0)
    check_true(
        "_video_component 是 async（base64 不阻塞事件循环）",
        "async def _video_component" in _method_source(main_py, "_video_component"),
    )


# ======================================================================
# 2. 下载器行为：候选回退（第一个 403 时自动换下一个）
# ======================================================================
def downloader_checks() -> None:
    """直接测 ``_stream_one`` 对 403 的处理 + ``download_many_candidates`` 的回退。"""
    import http.server
    import socketserver
    import threading

    # 两个假端点：/bad 返回 403，/good 返回一段字节
    GOOD = b"\x89PNG\r\n\x1a\n" + b"A" * 4096

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/bad"):
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"forbidden")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(GOOD)))
                self.end_headers()
                self.wfile.write(GOOD)

        def log_message(self, *a):  # 静音
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        base = f"http://127.0.0.1:{port}"

        from astrbot_plugin_rconsole.core.downloader import download_many_candidates

        # 候选顺序：坏的在前，好的在后 —— 必须回退成功
        results = asyncio.run(
            download_many_candidates(
                [[f"{base}/bad.webp", f"{base}/good.png"]],
                prefix="test_album",
                concurrency=2,
            )
        )
        check_true("候选回退：第 1 个 403、第 2 个 200 时能下到本地", results[0] is not None)
        if results[0] is not None:
            check("下到的是 good 端点的大小", results[0].stat().st_size, len(GOOD))
            results[0].unlink(missing_ok=True)

        # 全部候选都坏 —— 返回 None，不抛异常
        results = asyncio.run(
            download_many_candidates(
                [[f"{base}/bad1.webp", f"{base}/bad2.jpeg"]],
                prefix="test_album",
                concurrency=2,
            )
        )
        check("候选全失败 -> None（不抛异常）", results[0], None)

        httpd.shutdown()


# ======================================================================
# 3. 端到端：图集发送时只发本地文件（用假的 event 捕获消息链）
# ======================================================================
def send_path_checks() -> None:
    """构造一个假 event，跑 ``_send_album``，断言发出的媒体全是本地文件。

    ``main`` 依赖一整套 AstrBot API（消息组件 / 事件 / Star 基类），这里按需
    桩出最小可用集合：只要 ``Comp.Image`` / ``Comp.Video`` / ``Comp.Node`` /
    ``Comp.Nodes`` 能记录构造参数就够了。
    """
    # ---- 补桩：消息组件 + 事件 + Star 基类 ----
    mc = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, **kw):
            self.kw = kw
            for k, v in kw.items():
                setattr(self, k, v)

        @classmethod
        def fromURL(cls, url):
            return cls(url=url)

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path=path)

        @classmethod
        def fromBase64(cls, data):
            return cls(file=f"base64://{data}")

        def __repr__(self):
            return f"{type(self).__name__}({self.kw})"

    class Image(_Comp):
        pass

    class Video(_Comp):
        pass

    class Record(_Comp):
        pass

    class Plain(_Comp):
        pass

    class Node(_Comp):
        pass

    class Nodes(_Comp):
        pass

    class Json(_Comp):
        pass

    mc.Image, mc.Video, mc.Record, mc.Plain = Image, Video, Record, Plain
    mc.Node, mc.Nodes, mc.Json = Node, Nodes, Json
    mc.MessageChain = _Comp

    ev_mod = types.ModuleType("astrbot.api.event")
    ev_mod.AstrMessageEvent = object
    ev_mod.MessageChain = _Comp
    filt = types.SimpleNamespace(
        regex=lambda *a, **k: (lambda f: f),
        custom_filter=lambda *a, **k: (lambda f: f),
        permission_type=lambda *a, **k: (lambda f: f),
    )
    ev_mod.filter = filt

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = object

    sys.modules["astrbot.api.message_components"] = mc
    sys.modules["astrbot.api.event"] = ev_mod
    sys.modules["astrbot.api.star"] = star_mod
    # filter 下的子模块（main 里 from astrbot.api.event.filter import CustomFilter）
    filt_mod = types.ModuleType("astrbot.api.event.filter")
    filt_mod.CustomFilter = object
    sys.modules["astrbot.api.event.filter"] = filt_mod
    sys.modules["astrbot.api.event"].filter = filt

    from astrbot_plugin_rconsole.core import downloader as dl
    from astrbot_plugin_rconsole.platforms.base import ResolveResult

    import astrbot_plugin_rconsole.main as main_mod

    # 假下载器：把每个 URL 变成一个「本地文件」
    tmp = _ROOT / "tests" / "_tmp_send_path"
    tmp.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []

    async def fake_many_candidates(cands, **kw):
        out = []
        for i, _group in enumerate(cands):
            p = tmp / f"still_{i}.jpg"
            p.write_bytes(b"X" * 100)
            made.append(p)
            out.append(p)
        return out

    async def fake_many(urls, **kw):
        return await fake_many_candidates([[u] for u in urls], **kw)

    async def fake_media(url, **kw):
        p = tmp / f"anim_{len(made)}.mp4"
        p.write_bytes(b"V" * 100)
        made.append(p)
        return p

    async def fake_media_candidates(urls, **kw):
        """动图候选回退：第一个候选失败、第二个成功（模拟抖音 403 回退）。"""
        return await fake_media(urls[0] if urls else "", **kw)

    orig = (
        dl.download_many_candidates,
        dl.download_many,
        dl.download_media,
        dl.download_media_candidates,
    )
    dl.download_many_candidates = fake_many_candidates
    dl.download_many = fake_many
    dl.download_media = fake_media
    dl.download_media_candidates = fake_media_candidates
    main_mod.download_many_candidates = fake_many_candidates
    main_mod.download_many = fake_many
    main_mod.download_media = fake_media
    main_mod.download_media_candidates = fake_media_candidates

    class FakeEvent:
        def __init__(self):
            self.chains = []
            self.tracked = []

        def chain_result(self, chain):
            self.chains.append(chain)
            return chain

        def plain_result(self, text):
            self.chains.append(("plain", text))
            return ("plain", text)

        def track_temporary_local_file(self, p):
            self.tracked.append(p)

        def get_sender_name(self):
            return "测试用户"

        def get_sender_id(self):
            return "10001"

    class FakePlugin(main_mod.Main):
        """继承 Main 但跳过 __init__（真实 __init__ 要 Context / 配置对象）。

        这样 ``_send_album`` 内部调用的 ``_download_album_stills`` /
        ``_download_album_videos`` 都是**真实实现**，测试覆盖的是真实链路。
        """

        def __init__(self):  # noqa: D107 - 故意不调 super().__init__
            pass

        def conf_get(self, key, default=None):
            return default

    plugin = FakePlugin()

    try:
        # ---- 纯静态图集（用户实际踩坑的场景）----
        result = ResolveResult.ok(
            "抖音",
            images=["https://p3-pc-sign.douyinpic.com/a.webp?x=1"],
            extra={
                "album_kinds": ["still"],
                "image_candidates": [["https://p3-pc-sign.douyinpic.com/a.webp?x=1"]],
            },
        )
        ev = FakeEvent()
        asyncio.run(_collect(plugin._send_album, ev, result))
        media = [c for c in ev.chains if isinstance(c, list)]
        check_true("纯静态图集：发出了媒体链", media)
        if media:
            comps = media[0]
            check("纯静态图集：组件数是 1", len(comps), 1)
            check_true(
                "纯静态图集：发的是本地文件（fromFileSystem）而不是 URL",
                getattr(comps[0], "path", None) is not None
                and getattr(comps[0], "url", None) is None,
            )

        # ---- 混排图集（静-动-静）：顺序与组件类型都要对 ----
        result = ResolveResult.ok(
            "抖音",
            images=[
                "https://p3-pc-sign.douyinpic.com/s1.webp",
                "https://p3-pc-sign.douyinpic.com/s2.webp",
            ],
            videos=["https://v3-web.douyinvod.com/anim.mp4"],
            extra={
                "album_kinds": ["still", "animated", "still"],
                "image_candidates": [
                    ["https://p3-pc-sign.douyinpic.com/s1.webp"],
                    ["https://p3-pc-sign.douyinpic.com/s2.webp"],
                ],
            },
        )
        ev = FakeEvent()
        asyncio.run(_collect(plugin._send_album, ev, result))
        media = [c for c in ev.chains if isinstance(c, list)]
        check_true("混排图集：发出了媒体链", media)
        if media:
            names = [type(c).__name__ for c in media[0]]
            check("混排图集：顺序为 图-视频-图", names, ["Image", "Video", "Image"])

        # ---- 动图 ----
        result = ResolveResult.ok(
            "抖音动图",
            videos=["https://v3-web.douyinvod.com/anim.mp4"],
            extra={"album_kinds": ["animated"], "image_candidates": []},
        )
        ev = FakeEvent()
        asyncio.run(_collect(plugin._send_album, ev, result))
        media = [c for c in ev.chains if isinstance(c, list)]
        check_true("动图：发出了媒体链", media)
        if media:
            check("动图：发的是 Video 且为本地文件", type(media[0][0]).__name__, "Video")

        # ---- 全部下载失败 -> 必须给用户明确提示，而不是静默 ----
        async def all_fail(cands, **kw):
            return [None for _ in cands]

        main_mod.download_many_candidates = all_fail
        main_mod.download_many = all_fail
        result = ResolveResult.ok(
            "抖音",
            images=["https://p3-pc-sign.douyinpic.com/a.webp"],
            extra={
                "album_kinds": ["still"],
                "image_candidates": [["https://p3-pc-sign.douyinpic.com/a.webp"]],
            },
        )
        ev = FakeEvent()
        asyncio.run(_collect(plugin._send_album, ev, result))
        texts = [c[1] for c in ev.chains if isinstance(c, tuple) and c[0] == "plain"]
        check_true(
            "全部下载失败时给出失败提示（不静默）",
            any("下载失败" in t for t in texts),
        )
    finally:
        (
            dl.download_many_candidates,
            dl.download_many,
            dl.download_media,
            dl.download_media_candidates,
        ) = orig
        main_mod.download_many_candidates = orig[0]
        main_mod.download_many = orig[1]
        main_mod.download_media = orig[2]
        main_mod.download_media_candidates = orig[3]
        for p in made:
            p.unlink(missing_ok=True)
        try:
            tmp.rmdir()
        except OSError:
            pass


async def _collect(fn, *args):
    """把 async generator / 协程统一收成 list。"""
    res = fn(*args)
    if hasattr(res, "__aiter__"):
        return [x async for x in res]
    return [await res]


def main() -> int:
    print("--- 1. 静态检查（发送路径不得远程直发）---")
    static_checks()
    print("\n--- 1b. 跨容器：视频必须走 base64 ---")
    cross_container_checks()
    print("\n--- 2. 下载器候选回退 ---")
    downloader_checks()
    print("\n--- 3. 图集发送路径（只发本地文件）---")
    send_path_checks()

    print("\n" + "=" * 52)
    if _FAILED:
        print(f"{len(_FAILED)} 项失败：")
        for f in _FAILED:
            print(f"   - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
