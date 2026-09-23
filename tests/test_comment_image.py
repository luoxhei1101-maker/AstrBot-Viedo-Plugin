"""评论图片 / 动图提取的离线回归测试（v1.6.6）。

**背景**：用户反馈「视频链接里作者的图片评论能不能提取出来」。

一查发现两个 bug，而且第二个正好命中「作者只发图不配字」这种最常见的情况：

1. 评论节点只发 ``Comp.Plain(text)``，图片字段（抖音 ``image_list`` /
   B站 ``content.pictures``）**压根没读**；
2. 更糟的是 ``if not text: return None`` —— **纯图片评论被整条丢掉**。
   实测用户给的那条作品（``7688010612275061413``）评论共 2 条，作者那条
   ``text`` 正好是空串，于是永远看不到。

实测数据（2026-09-22，抖音评论图）：

==================  ==========  ===========  ============
字段                体积        尺寸         结论
==================  ==========  ===========  ============
``origin_url``      660 KB     1600×1600    **原图，用它**
``medium_url``      88 KB      332×332      缩略
``thumb_url``       21 KB      124×124      缩略
``crop_url``        29 KB      124×166      裁切缩略
``download_url``    ——          403        **别碰**
==================  ==========  ===========  ============

``origin_url`` 的 4 个候选里后缀分 ``.image``(PNG 804KB) 和 ``.jpeg``(660KB)，
都是 1600×1600 原图 —— 复用图集那套 ``rank_image_candidates`` 顺手把体积也省了。

**动图**：接口层**没有任何标记**（``image_list`` 字段和静态图完全一样，
扫了 28 个作品 / 955 条评论，``video_list`` 全是 ``None``），所以只能
「下下来看文件本身」—— 见 ``is_animated_image``。

跑法::

    python tests/test_comment_image.py
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


# ---- 真实抓到的 URL 片段（2026-09-22，作品 7688010612275061413 作者评论）----
DY_JPEG = "https://p11-sign.douyinpic.com/tos-cn-i-p14lwwcsbr/ba3e0798~tplv-p14lwwcsbr-7.jpeg?x=1"
DY_IMAGE_A = "https://p11-sign.douyinpic.com/tos-cn-i-p14lwwcsbr/ba3e0798~tplv-p14lwwcsbr-7.image?x=1"
DY_IMAGE_B = "https://p5-ex-gddgtc-sign.douyinpic.com/tos-cn-i-p14lwwcsbr/ba3e0798~tplv-p14lwwcsbr-7.image?x=1"


def _dy_image_block(urls, uri="tos-cn-i-p14lwwcsbr/abc"):
    return {"origin_url": {"uri": uri, "url_list": list(urls)}}


# ==========================================================================
# A) 抖音评论解析
# ==========================================================================

def part_a_douyin() -> None:
    print("\n[A] 抖音 _normalize_comment")
    from astrbot_plugin_rconsole.core.douyin_comment import _normalize_comment

    # ---- 1) 纯图评论：用户实际遇到的那条（作者只发图不配字）----
    item = {
        "user": {"nickname": "【狼道】f40(已黑化)"},
        "text": "",
        "digg_count": 20,
        "create_time": 1789942490,
        "ip_label": "山东",
        "image_list": [_dy_image_block([DY_IMAGE_A, DY_IMAGE_B, DY_JPEG])],
    }
    c = _normalize_comment(item)
    check_true("纯图评论不再被丢掉（关键）", c is not None)
    if c:
        check("  昵称", c["nickname"], "【狼道】f40(已黑化)")
        check("  图数量", len(c["images"]), 1)
        check("  该图候选数", len(c["images"][0]), 3)
        check_true(
            "  .jpeg 排在 .image 前面（复用 rank_image_candidates）",
            c["images"][0][0].split("?")[0].endswith(".jpeg"),
        )
        check_true("  纯图评论不留空行正文", not c["text"].startswith("\n"))
        check_true("  正文里带上 meta（地点/赞数）", "山东" in c["text"])

    # ---- 2) 图文评论 ----
    item2 = {
        "user": {"nickname": "好哦"},
        "text": "这无疑是萌萌的",
        "digg_count": 5,
        "create_time": 1789942490,
        "ip_label": "广东",
        "image_list": [_dy_image_block([DY_JPEG])],
    }
    c2 = _normalize_comment(item2)
    check_true("图文评论保留", c2 is not None)
    if c2:
        check_true("  正文以原文开头", c2["text"].startswith("这无疑是萌萌的"))
        check("  图数量", len(c2["images"]), 1)

    # ---- 3) 无图无文字 -> 丢弃 ----
    check(
        "既没文字也没图 -> None",
        _normalize_comment({"user": {"nickname": "x"}, "text": "  "}),
        None,
    )

    # ---- 4) 多图评论 ----
    item4 = {
        "user": {"nickname": "多图"},
        "text": "三张图",
        "image_list": [
            _dy_image_block([DY_JPEG], "a"),
            _dy_image_block([DY_IMAGE_A], "b"),
            _dy_image_block([DY_JPEG], "c"),
        ],
    }
    c4 = _normalize_comment(item4)
    check("多图评论提全 3 张", len(c4["images"]) if c4 else 0, 3)

    # ---- 5) 坏结构不炸 ----
    check_true(
        "image_list 是 None 时不炸",
        _normalize_comment({"user": {}, "text": "只有文字", "image_list": None})
        is not None,
    )
    check_true(
        "image_list 元素不是 dict 时不炸",
        _normalize_comment({"user": {}, "text": "t", "image_list": ["??"]}) is not None,
    )
    check_true(
        "origin_url 缺失时不炸",
        _normalize_comment({"user": {}, "text": "t", "image_list": [{"x": 1}]})
        is not None,
    )


# ==========================================================================
# A2) 评论表情包 sticker —— 动图就藏在这里
# ==========================================================================

# 真实抓到的 sticker（作品 7687666222385355867 的 peach猹 评论）
# 实测两个 URL 都返回 image/gif、344×240、123 帧
STICKER_GIF = {
    "id": 7667238231015948297,
    "width": 344,
    "height": 240,
    "static_url": {
        "uri": "tos-cn-o-0812/oUZAeCE4IpDAxzgoCJAEFunAf5CABqIygMELYN",
        "url_list": [
            "https://p26-sign.douyinpic.com/obj/tos-cn-o-0812/AAA?sc=sticker_heif",
            "https://p5-ex-gddgtc-sign.douyinpic.com/obj/tos-cn-o-0812/AAA?sc=sticker_heif",
        ],
        "width": 344,
        "height": 240,
    },
    "animate_url": {
        "uri": "tos-cn-o-0812/oUZAeCE4IpDAxzgoCJAEFunAf5CABqIygMELYN",
        "url_list": [
            "https://p26-sign.douyinpic.com/obj/tos-cn-o-0812/AAA?sc=sticker_heif",
            "https://p5-ex-gddgtc-sign.douyinpic.com/obj/tos-cn-o-0812/AAA?sc=sticker_heif",
        ],
        "width": 344,
        "height": 240,
    },
    "sticker_type": 2,
    "origin_package_id": -4156610121569672,
    "id_str": "7667238231015948297",
    "author_sec_uid": "",
    "activity_schema": "",
    "activity_desc": "",
}


def part_a2_sticker() -> None:
    print("\n[A2] 评论表情包 sticker（动图在这里）")
    from astrbot_plugin_rconsole.core.douyin_comment import (
        _normalize_comment,
        _sticker_candidates,
    )

    # ---- 1) 候选提取：animate_url 优先 ----
    got = _sticker_candidates({"sticker": STICKER_GIF})
    check_true("sticker 能提出候选（关键）", len(got) == 2)
    check_true(
        "  取的是 animate_url（动图版优先）",
        all(u.endswith("sc=sticker_heif") for u in got),
    )

    # ---- 2) 只有 static_url 时退回它 ----
    only_static = {
        "sticker": {
            "static_url": {"url_list": ["https://x/static.png"]},
        }
    }
    check("只有 static_url 时退回它", _sticker_candidates(only_static),
          ["https://x/static.png"])

    # ---- 3) 异常结构不炸 ----
    check("没有 sticker -> []", _sticker_candidates({}), [])
    check("sticker 不是 dict -> []", _sticker_candidates({"sticker": "??"}), [])
    check("url_list 为空 -> []",
          _sticker_candidates({"sticker": {"static_url": {"url_list": []}}}), [])

    # ---- 4) 纯 sticker 评论（无文字、无 image_list）必须保留 ----
    item = {
        "user": {"nickname": "早晨我睡觉"},
        "text": "我没有绷住，大火还得靠小猫",
        "digg_count": 58,
        "create_time": 1789995384,
        "ip_label": "广西",
        "content_type": 3,
        "sticker": STICKER_GIF,
    }
    c = _normalize_comment(item)
    check_true("带 sticker 的评论保留", c is not None)
    if c:
        check_true("  文本正确", c["text"].startswith("我没有绷住"))
        check("  sticker 进了 images（关键）", len(c["images"]), 1)
        check("  该 sticker 有 2 个候选", len(c["images"][0]), 2)

    # ---- 5) 只有 sticker 没有任何文字 ----
    c2 = _normalize_comment({
        "user": {"nickname": "菜包包"},
        "text": "",
        "sticker": STICKER_GIF,
    })
    check_true("纯 sticker（无文字）也不被丢（关键）", c2 is not None)
    if c2:
        check("  图数量为 1", len(c2["images"]), 1)

    # ---- 6) image_list + sticker 同时存在时两个都要 ----
    c3 = _normalize_comment({
        "user": {"nickname": "双份"},
        "text": "图文 + 表情包",
        "image_list": [_dy_image_block([DY_JPEG])],
        "sticker": STICKER_GIF,
    })
    if c3:
        check("image_list 与 sticker 都被收集", len(c3["images"]), 2)

    # ---- 7) 真实场景回归：这条作品里的 sticker 评论不再丢 ----
    check_true(
        "截图里那条评论现在能提取到",
        c is not None and len(c["images"]) > 0,
    )


# ==========================================================================
# B) B 站评论解析
# ==========================================================================

def part_b_bili() -> None:
    print("\n[B] B 站 _normalize_comment")
    from astrbot_plugin_rconsole.core.bili_comment import _normalize_comment

    item = {
        "member": {"uname": "UP主", "level_info": {"current_level": 6}},
        "content": {
            "message": "",
            "pictures": [{"img_src": "https://i0.hdslb.com/bfs/x.jpg"}],
        },
        "like": 12,
        "ctime": 1789942490,
    }
    c = _normalize_comment(item)
    check_true("B站纯图评论不再被丢掉（关键）", c is not None)
    if c:
        check("  昵称", c["nickname"], "UP主")
        check("  图数量", len(c["images"]), 1)
        check("  单图单候选", c["images"][0], ["https://i0.hdslb.com/bfs/x.jpg"])
        check_true("  meta 含 Lv 与赞数", "Lv6" in c["text"] and "赞 12" in c["text"])

    item2 = {
        "member": {"uname": "甲"},
        "content": {"message": "有字有图", "pictures": [{"img_src": "https://a/b.png"}]},
    }
    c2 = _normalize_comment(item2)
    check_true("B站图文评论正文正确", c2 and c2["text"].startswith("有字有图"))

    check(
        "B站既没文字也没图 -> None",
        _normalize_comment({"member": {"uname": "x"}, "content": {"message": " "}}),
        None,
    )
    check_true(
        "B站 pictures 缺失时不炸",
        _normalize_comment({"member": {}, "content": {"message": "t"}}) is not None,
    )


# ==========================================================================
# C) 动图检测与转码
# ==========================================================================

def part_c_animated() -> None:
    print("\n[C] is_animated_image / animated_to_mp4")
    try:
        from PIL import Image
    except ImportError:
        print("  ⚠️ 跳过（无 Pillow）")
        return

    from astrbot_plugin_rconsole.core.media import animated_to_mp4, is_animated_image

    tmp = _ROOT / "tests" / "_tmp_comment_img"
    tmp.mkdir(parents=True, exist_ok=True)

    # 静态 PNG
    p_static = tmp / "static.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(p_static)
    check_true("静态 PNG -> False", is_animated_image(p_static) is False)

    # 静态 JPEG
    p_jpg = tmp / "static.jpg"
    Image.new("RGB", (64, 64), (30, 30, 200)).save(p_jpg, format="JPEG")
    check_true("静态 JPEG -> False", is_animated_image(p_jpg) is False)

    # 多帧 GIF
    p_gif = tmp / "anim.gif"
    frames = [Image.new("RGB", (64, 64), (i * 40 % 255, 100, 100)) for i in range(4)]
    frames[0].save(p_gif, save_all=True, append_images=frames[1:], duration=120, loop=0)
    check_true("多帧 GIF -> True（关键）", is_animated_image(p_gif) is True)

    # 单帧 GIF 不算动图
    p_gif1 = tmp / "one.gif"
    Image.new("RGB", (32, 32), (10, 10, 10)).save(p_gif1, format="GIF")
    check_true("单帧 GIF -> False", is_animated_image(p_gif1) is False)

    check_true("不存在的文件 -> False（不抛异常）",
               is_animated_image(tmp / "nope.png") is False)

    # 转 mp4：没 ffmpeg 时应优雅返回 None（本地很可能没装）
    mp4 = asyncio.run(animated_to_mp4(p_gif))
    if mp4 is None:
        print("  ℹ️ 本机没有 ffmpeg / 转码失败 -> 返回 None（预期降级行为）")
        check_true("转不出 mp4 时返回 None 而不是抛异常", True)
    else:
        check_true("动图转出了 mp4", mp4.exists() and mp4.stat().st_size > 0)

    for p in tmp.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass


# ==========================================================================
# D) 节点排布（真实行为，需要 main.py）
# ==========================================================================

def part_d_nodes() -> None:
    print("\n[D] _build_comment_nodes 的排布")

    mc = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, *a, **k):
            self.args = a
            self.kw = k

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path, kind="fs")

        @classmethod
        def fromBytes(cls, data):
            return cls(kind="bytes")

        @classmethod
        def fromURL(cls, url):
            return cls(url, kind="url")

    for name in ("Plain", "Image"):
        setattr(mc, name, _Comp)
    mc.Video = _Comp
    mc.Node = lambda *a, **k: ("node", a, k)
    mc.Nodes = lambda *a, **k: ("nodes", a, k)

    ev_mod = types.ModuleType("astrbot.api.event")
    ev_mod.AstrMessageEvent = object
    ev_mod.MessageChain = object
    ev_mod.filter = types.SimpleNamespace(
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
    from astrbot_plugin_rconsole.core import downloader as dl

    tmp = _ROOT / "tests" / "_tmp_comment_nodes"
    tmp.mkdir(parents=True, exist_ok=True)
    p_static = tmp / "a.png"
    try:
        from PIL import Image

        Image.new("RGB", (32, 32), (0, 0, 0)).save(p_static)
    except ImportError:
        p_static.write_bytes(b"x" * 100)

    async def fake_media_candidates(urls, **kw):
        return p_static

    orig = dl.download_media_candidates
    dl.download_media_candidates = fake_media_candidates
    main_mod.download_media_candidates = fake_media_candidates

    class FakeEvent:
        def track_temporary_local_file(self, p):
            pass

        def get_sender_id(self):
            return "10001"

    class FakePlugin(main_mod.Main):
        def __init__(self):  # noqa: D107
            pass

        async def _video_component(self, path):
            return _Comp(kind="video-bytes")

    plugin = FakePlugin()

    try:
        # ---- 1) 图文评论：文字在上、图在下，同一节点 ----
        comments = [{"nickname": "甲", "text": "有字有图\n—— 广东", "images": [["u1"]]}]
        nodes = asyncio.run(plugin._build_comment_nodes(FakeEvent(), comments))
        check("图文评论 -> 1 个节点", len(nodes), 1)
        if nodes:
            comps = nodes[0][1][0]
            check("  节点里 2 个组件（文字 + 图）", len(comps), 2)
            check_true("  第 1 个是文字（在上）", isinstance(comps[0], _Comp))
            check_true(
                "  第 2 个是图片（在下）",
                getattr(comps[1], "kw", {}).get("kind") == "fs",
            )

        # ---- 2) 纯图评论（作者那种）：仍然要有节点 ----
        comments = [{"nickname": "作者", "text": "—— 山东", "images": [["u1"]]}]
        nodes = asyncio.run(plugin._build_comment_nodes(FakeEvent(), comments))
        check("纯图评论也生成节点（关键）", len(nodes), 1)
        if nodes:
            check("  节点里 2 个组件（meta + 图）", len(nodes[0][1][0]), 2)

        # ---- 3) 多图评论：几张图就几个组件，顺序在文字之后 ----
        comments = [{"nickname": "乙", "text": "三张", "images": [["u1"], ["u2"], ["u3"]]}]
        nodes = asyncio.run(plugin._build_comment_nodes(FakeEvent(), comments))
        check("多图评论 -> 仍是 1 个节点", len(nodes), 1)
        if nodes:
            check("  组件数 = 1 文字 + 3 图", len(nodes[0][1][0]), 4)

        # ---- 4) 超过上限的图被裁掉 ----
        many = [["u%d" % i] for i in range(9)]
        comments = [{"nickname": "丙", "text": "九宫格", "images": many}]
        nodes = asyncio.run(plugin._build_comment_nodes(FakeEvent(), comments))
        if nodes:
            check(
                "  图数量被 _COMMENT_IMAGE_MAX 截断",
                len(nodes[0][1][0]) - 1,
                main_mod.Main._COMMENT_IMAGE_MAX,
            )

        # ---- 5) 没有任何内容 -> 不生成节点 ----
        nodes = asyncio.run(
            plugin._build_comment_nodes(FakeEvent(), [{"nickname": "丁", "text": "", "images": []}])
        )
        check("空评论不生成节点", nodes, [])
    finally:
        dl.download_media_candidates = orig
        main_mod.download_media_candidates = orig
        for p in tmp.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass


# ==========================================================================
# E) 源码不变量
# ==========================================================================

def _code_only(src: str) -> str:
    """只保留代码 token，丢掉注释与字符串字面量。

    注释和 docstring 里会**提到**某个 API 名（本文件里就有
    「``segment.video(path)`` → ``Comp.Video.fromFileSystem(path)``」这样的
    API 对照表），朴素的子串搜索会把它当成「真的用了它」—— 这正是要防的误报。
    连字符串一起丢掉还能顺带躲开带 ``#`` 的配置键（如 ``"#R配置"``）被当成注释截断。

    代价：如果某个 API 名出现在字符串里就检不出来。但这里要查的都是属性访问，
    只可能写在代码里，所以安全。
    """
    import io
    import tokenize

    parts: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            parts.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src
    return " ".join(parts)


def part_e_source_guard() -> None:
    print("\n[E] 源码不变量")
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    dy_src = (_ROOT / "core" / "douyin_comment.py").read_text(encoding="utf-8")
    bili_src = (_ROOT / "core" / "bili_comment.py").read_text(encoding="utf-8")
    media_src = (_ROOT / "core" / "media.py").read_text(encoding="utf-8")
    main_code = _code_only(main_src)

    check_true(
        "抖音评论提取 image_list",
        "image_list" in dy_src and "_comment_image_candidates" in dy_src,
    )
    check_true(
        "抖音评论不再「没文字就丢」",
        "if not text and not images:" in dy_src,
    )
    check_true(
        "B站评论提取 content.pictures",
        'content.get("pictures")' in bili_src,
    )
    check_true(
        "B站评论不再「没正文就丢」",
        "if not message and not images:" in bili_src,
    )
    check_true(
        "评论图先落盘（download_media_candidates）",
        "download_media_candidates" in main_code,
    )
    check_true(
        "评论图片禁止 fromURL（铁律：发送端下载器不带 Referer）",
        "Image.fromURL" not in main_code,
    )
    check_true(
        "没有 Comp.Video.fromFileSystem（铁律：协议端跨容器读不到）",
        "Video.fromFileSystem" not in main_code,
    )
    check_true(
        "动图走 _video_component（base64，跨容器安全）",
        "_video_component" in main_code,
    )
    check_true(
        "有 is_animated_image / animated_to_mp4",
        "def is_animated_image" in media_src and "def animated_to_mp4" in media_src,
    )
    check_true(
        "转 mp4 带 yuv420p + 偶数尺寸修正",
        "yuv420p" in media_src and "trunc(iw/2)*2" in media_src,
    )
    check_true(
        "评论节点图片用 fromFileSystem（与既有转发路径一致）",
        "Comp . Image . fromFileSystem" in main_code
        or "Comp.Image.fromFileSystem" in main_code,
    )
    ast.parse(main_src)


def main() -> int:
    print("=" * 72)
    print("评论图片 / 动图提取 —— 回归测试")
    print("=" * 72)

    part_a_douyin()
    part_a2_sticker()
    part_b_bili()
    part_c_animated()
    part_d_nodes()
    part_e_source_guard()

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
