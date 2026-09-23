"""官机 markdown 的**排版**回归测试：文案 / 评论 / 图集怎么放才不被当标题。

背景（都是实测踩出来的，不是理论）：

1. **图片之间必须空行分隔** —— 官方文档「换多行」写明单换行不换行；三行
   ``![…]`` 紧贴时被当成同一段文本，手机 QQ 只渲染第一张。
2. **作品文案不能当标题** —— markdown 的 ``#`` 是标题语法，而抖音文案几乎
   全是 ``#话题``、还分多行，塞进正文时每一行都变成标题，排版垮掉。
3. **评论整条包进代码框** —— 评论正文里什么都有（``#`` / ``*`` / ``~~``），
   官机又没有合并转发，只能靠代码框原样呈现；框的「语言标记」位置放昵称。
4. **官机多图必须走 MD** —— 官机没有合并转发，``total > max_images`` 时
   只能「只发前 N 张」（用户实测：三张图的图集只出来一张，他的
   ``plugin.max_images`` 是 1）。markdown 内嵌图不看那个阈值。

跑法::

    python tests/test_md_layout.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

FAILED: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}\n       期望: {want!r}\n       实际: {got!r}")
        FAILED.append(label)


def check_true(label: str, cond, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}" + (f"\n       {detail}" if detail else ""))
        FAILED.append(label)


def _load_main():
    import astrbot_plugin_rconsole.main as main_mod

    class _Conf(dict):
        def save_config(self):
            pass

    class P(main_mod.Main):
        def __init__(self, data=None):  # noqa: D107
            self.conf_data = _Conf(data or {})

    return main_mod, P


class _Ev:
    """最小事件桩：够 conf_plat / 平台能力探测用。"""

    def __init__(self, platform: str = "qqofficial"):
        self._platform = platform
        self.unified_msg_origin = "test:GroupMessage:1"

    def get_platform_name(self):
        return self._platform

    def get_platform_id(self):
        return f"t_{self._platform}"

    def get_message_str(self):
        return ""


class _Result:
    """最小 ResolveResult 桩。"""

    def __init__(self, **kw):
        self.platform = kw.get("platform", "抖音")
        self.author = kw.get("author", "卡卡（反迷你）")
        self.title = kw.get("title", "")
        self.images = kw.get("images", [])
        self.videos = kw.get("videos", [])
        self.audios = kw.get("audios", [])
        self.extra = kw.get("extra", {})


# ==========================================================================
# A) 评论：代码框包裹、昵称当框标题、不带图
# ==========================================================================

def part_a_comment_md() -> None:
    print("\n[A] 评论 → 一条 MD（代码框 + 昵称做框标题，不带图）")
    _, P = _load_main()
    plugin = P()

    comments = [
        {"nickname": "小明", "text": "哈哈#搞笑\n—— 09-23 10:00 · 赞 12"},
        {"nickname": "小 红", "text": "这里有三反引号 ``` 要处理"},
        {"nickname": "纯图君", "text": "—— 09-23 11:00"},
    ]
    md = plugin._comment_md_text(comments, "抖音")

    check_true("有固定标题", md.startswith("# 💬 抖音 · 评论（3 条）"), md[:40])
    check_true(
        "昵称放在**代码框的语言标记位置**（客户端会渲染成框标题）",
        "```小明" in md,
        "用户指定：` ```名字 ` 的形式，比正文里再写【名字】省版面",
    )
    check_true("昵称里的空格被清掉（语言标记不能含空白）", "```小红" in md)
    check_true("正文原样保留（# 号没被改写）", "哈哈#搞笑" in md)
    check_true("时间与赞数也在", "赞 12" in md)
    check_true(
        "正文里的三反引号被替换（不会提前闭合代码框）",
        "'''" in md and "``` 要处理" not in md,
        "否则代码框会被正文里的 ``` 提前结束",
    )
    check("三个代码框（每条评论一个）", md.count("```"), 6)
    check_true("完全没有图片 markdown", "![" not in md, "官机评论不带图/动图")

    # 边界
    check("空列表 -> 空串", plugin._comment_md_text([], "抖音"), "")
    check(
        "昵称和正文都空 -> 空串",
        plugin._comment_md_text([{"nickname": "", "text": ""}], "抖音"),
        "",
    )
    only_name = plugin._comment_md_text([{"nickname": "只有名字", "text": ""}], "抖音")
    check_true("只有昵称也保留（做框标题）", "```只有名字" in only_name)


# ==========================================================================
# B) 图集：标题用固定文字、文案进代码框、图之间空行
# ==========================================================================

def part_b_album_md() -> None:
    print("\n[B] 图集 MD：文案不当标题、图不挤在一起")
    _, P = _load_main()
    plugin = P()
    ev = _Ev()

    title = "一直说说说。#小猫 #罗小黑 #皇受 #同人 #抽象\n#暗区跳舞 #猎奇"
    images = ["https://p3.douyinpic.com/a.jpg", "https://p3.douyinpic.com/b.jpg"]
    result = _Result(
        title=title, images=images, extra={"image_sizes": [(1612, 1538), (900, 1600)]}
    )
    md = asyncio.run(plugin._gallery_md_text(ev, result, images))

    check_true("标题是固定文字（平台名 + 类型）", md.startswith("# 抖音 · 图集"), md[:40])
    check_true(
        "作品文案没有被拼进标题",
        "# 一直说说说" not in md,
        "文案以 # 开头时会被渲染成大标题，整片排版垮掉",
    )
    check_true("文案进代码框、原样保留", f"```标题\n{title}\n```" in md)
    check_true("作者用引用块", "> 卡卡（反迷你）" in md)
    check_true("两张图都在", md.count("![图") == 2)
    check_true(
        "图片之间空行分隔",
        "\n\n![图2" in md,
        "单换行时手机只渲染第一张",
    )

    # 缺尺寸 -> 整体放弃
    no_size = _Result(title="t", images=["https://x/1.jpg"], extra={"image_sizes": []})
    check(
        "缺尺寸且不探 -> None（退回逐条）",
        asyncio.run(plugin._gallery_md_text(ev, no_size, no_size.images)),
        None,
    )

    # 空图集
    check("没有图片 -> None", asyncio.run(plugin._gallery_md_text(ev, _Result(), [])), None)

    # 超过上限
    many = [f"https://x/{i}.jpg" for i in range(9)]
    big = _Result(images=many, extra={"image_sizes": [(100, 100)] * 9})
    check(
        "超过 limit -> None（调用方负责分批）",
        asyncio.run(plugin._gallery_md_text(ev, big, many)),
        None,
    )
    part = asyncio.run(
        plugin._gallery_md_text(ev, big, many[:3], limit=3, head=False)
    )
    check_true("head=False 时不重复标题", part and not part.startswith("#"), str(part)[:40])

    # 文案里的三反引号同样要处理
    tricky = _Result(
        title="带 ``` 的文案",
        images=["https://x/1.jpg"],
        extra={"image_sizes": [(100, 100)]},
    )
    tricky_md = asyncio.run(plugin._gallery_md_text(ev, tricky, tricky.images))
    check_true("文案里的 ``` 被替换", "'''" in tricky_md)


# ==========================================================================
# C2) 解析简介 MD：文案进代码框、标记写「标题」
# ==========================================================================

def part_c2_intro_md() -> None:
    print("\n[C2] 解析简介 MD：文案进代码框（标记＝「标题」）")
    _, P = _load_main()
    plugin = P()

    title = "一直说说说。#小猫 #罗小黑\n#暗区跳舞 #猎奇"
    # 注意带上 images —— 真实场景里简介的对象总有媒体，类型那一栏才拼得出来
    result = _Result(title=title, author="卡卡（反迷你）", images=["https://x/1.jpg"])
    md = plugin._build_intro_md(result)

    check_true("标题是固定文字（平台 · 类型）", md.startswith("# 抖音 · 图集"), md[:44])
    check_true("文案没被拼进标题", "# 一直说说说" not in md)
    check_true(
        "代码框的标记写「标题」",
        f"```标题\n{title}\n```" in md,
        "客户端会把语言标记渲染成框标题 —— 用户指定",
    )
    check_true("作者用引用块", "> 卡卡（反迷你）" in md)

    check("类型/标题/作者全空 -> None（调用方退回纯文本）",
          plugin._build_intro_md(_Result(title="", author="")), None)
    only_author = plugin._build_intro_md(_Result(title="", author="某人"))
    check_true("只有作者也能拼", bool(only_author) and "> 某人" in only_author)
    tricky = plugin._build_intro_md(_Result(title="带 ``` 的文案", author=""))
    check_true("文案里的 ``` 被替换", "'''" in tricky)

    # 图集 / 多图会自己发带标题的 MD —— 简介要跳过，否则标题重复两条
    ev = _Ev("qqofficial")
    check_true(
        "官机 + 图集 -> 自己发 MD（简介跳过）",
        plugin._will_send_gallery_md(
            ev, _Result(extra={"album_kinds": ["still", "still"]})
        ),
    )
    check_true(
        "官机 + 多图 -> 自己发 MD（简介跳过）",
        plugin._will_send_gallery_md(ev, _Result(images=["a", "b"])),
    )
    check(
        "官机 + 单图 -> 不发 MD（简介照发）",
        plugin._will_send_gallery_md(ev, _Result(images=["a"])),
        False,
    )
    check(
        "OneBot 永远不走 MD",
        plugin._will_send_gallery_md(
            _Ev("aiocqhttp"),
            _Result(extra={"album_kinds": ["still"]}, images=["a", "b"]),
        ),
        False,
    )


# ==========================================================================
# C) 源码不变量
# ==========================================================================

def part_c_source() -> None:
    print("\n[C] 源码不变量")
    src = (_ROOT / "main.py").read_text(encoding="utf-8")

    check_true("有 _comment_md_text", "def _comment_md_text" in src)
    check_true(
        "官机评论走 MD 分支（B站 / 抖音各一处）",
        src.count("_comment_md_text(comments") == 2,
    )
    check_true(
        "官机评论分支在合并转发**之前**",
        src.index("if self._caps(event).markdown:")
        < src.index("nodes = await self._build_comment_nodes(event, comments)"),
        "官机没有 Nodes 消息段，必须先分流",
    )

    # 通用 MD 拼装 + _send_images 接入
    check_true("有通用的 _gallery_md_text", "async def _gallery_md_text" in src)
    check_true(
        "_send_images 里有官机 MD 分支（不受 max_images 限制）",
        "if self._caps(event).markdown and total:" in src,
        "否则三张图的快照图集会被砍成一张",
    )
    check_true(
        "_send_images 的 MD 分支支持分批",
        "chunks = [urls[i:i + chunk_size] for i in range(0, total, chunk_size)]" in src,
    )
    check_true(
        "抖音图集也走同一个拼装入口",
        "await self._gallery_md_text(event, result, list(result.images))" in src,
    )
    check_true("旧的同步版 _album_md_text 已移除", "def _album_md_text" not in src)

    # 简介（解析文案）也走 MD + 代码框
    check_true("有 _build_intro_md", "def _build_intro_md" in src)
    check_true("有 _will_send_gallery_md", "def _will_send_gallery_md" in src)
    check_true(
        "简介在官机走 MD，图集/多图时跳过（避免标题重复两条）",
        "if show_desc and not self._will_send_gallery_md(event, result):" in src,
    )
    check_true(
        "纯文本 _build_intro 保持不动",
        "def _build_intro(self, result: ResolveResult, prefix: str) -> str:" in src,
    )
    check_true(
        "图集 MD 的文案标记也是「标题」",
        'f"```标题\\n{safe_title}\\n```"' in src,
        "和 _build_intro_md 保持同一形态",
    )

    # ---- 菜单：随机图 → 图床唯一链 → 一条 MD（图 + 按钮）----
    check_true("有 _menu_image_md", "async def _menu_image_md" in src)
    read_size = src.find("PILImage.open(BytesIO(body))")
    upload = src.find("upload_image(body")
    check_true(
        "先读真实尺寸、再上传图床（顺序不能反）",
        read_size != -1 and upload != -1 and read_size < upload,
        "随机图 API 每次给的图都不一样，尺寸只能本地读出来",
    )
    check_true(
        "降级链齐全：随机图 → 纯文字 MD → 本地渲染图",
        "_menu_markdown(bot_name), rows" in src and "render_menu(" in src,
    )
    check_true(
        "有 _menu_image_api / _image_bed_key",
        "def _menu_image_api" in src and "def _image_bed_key" in src,
    )
    check_true(
        "随机图默认走竖屏档（比例更稳，实测极差 0.10）",
        "api.elaina.cat/random/mobile" in src,
    )

    # ---- 测试/自检类命令已下架 ----
    consts = (_ROOT / "core" / "constants.py").read_text(encoding="utf-8")
    gone = (
        "mdLayout", "mdImage", "platformTest", "platformCaps", "buttonsTest",
    )
    check_true(
        "五条自检命令已从规则表移除",
        all(k not in consts for k in gone),
        "不该再出现在 COMMAND_RULES 里",
    )
    check_true(
        "对应的 handler 也已删除",
        all(f"{n}" not in src for n in (
            "cmd_md_layout", "cmd_md_image", "cmd_platform_test",
            "cmd_platform_caps", "cmd_buttons_test",
        )),
    )


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 官机 markdown 排版回归测试")
    print("=" * 70)
    part_a_comment_md()
    part_b_album_md()
    part_c2_intro_md()
    part_c_source()

    print()
    print("=" * 70)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for item in FAILED:
            print("   -", item)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
