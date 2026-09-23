"""QQ 官方机器人「按钮」（keyboard）的离线回归测试（v1.6.9）。

**背景**：用户要「菜单图片下面挂一排可点的按钮」。

但 AstrBot 的 ``qqofficial`` 适配器**完全不支持 keyboard** ——
``_parse_to_qqofficial`` 只认 ``Plain / Image / Record / Video / File``，
其余一律 ``logger.debug("qq_official 忽略 ...")``，按钮组件会被静默丢掉。
所以按钮只能在插件里自己拼 payload、**绕过适配器直发**
（``main._send_qq_payload``）。

官方硬限制（``bot.q.qq.com`` 的「消息按钮」+「发送群聊消息」两页）：

========================  ==========================================
最多 **5 行**             每行最多 **5 个**按钮
``label`` ≤ **10 字符**   超了客户端会截断（不如自己截，至少可控）
``action.type``           0=跳转 / 1=回调 / 2=指令
``action.enter``          **仅单聊可用**；群里点击只把指令插进输入框
``permission.type``       0=指定用户 / 1=仅管理员 / 2=所有人
``action.reply``          指令是否带「引用本消息」发出
========================  ==========================================

这份测试盯两件事：

1. **纯数据构建**（``core/qq_buttons.py``）—— 硬限制必须真的被截断，
   否则按钮在客户端会**静默消失**，排查起来极其费劲；
2. **源码不变量** —— 「绕过适配器直发」和「按钮和 markdown 必须在同一条」
   这两条一旦被顺手改回去，功能就直接废了。

跑法::

    python tests/test_qq_buttons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))

from astrbot_plugin_rconsole.core.qq_buttons import (  # noqa: E402
    MAX_BUTTONS_PER_ROW,
    MAX_LABEL_CHARS,
    MAX_ROWS,
    button,
    inline_cmd,
    keyboard,
    link_button,
)

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
# A) 单个按钮
# ==========================================================================

def part_a_button() -> None:
    print("\n[A] button()")
    b = button("R菜单", "#R菜单", enter=True)
    check("label", b["render_data"]["label"], "R菜单")
    check("visited_label 与 label 一致", b["render_data"]["visited_label"], "R菜单")
    check("action.type = 2（指令按钮）", b["action"]["type"], 2)
    check("action.data 原样保留", b["action"]["data"], "#R菜单")
    check("enter 透传（点击直接发送）", b["action"]["enter"], True)
    check("默认 permission = 2（所有人可点）", b["action"]["permission"]["type"], 2)
    check("默认 reply = False（不带引用）", b["action"]["reply"], False)
    check_true("带 unsupport_tips（低版本客户端提示）",
               bool(b["action"]["unsupport_tips"]))

    # ---- 10 字符硬限制 ----
    long_label = "一二三四五六七八九十十一十二"
    check("超长 label 截断到 10 字符",
          len(button(long_label, "x")["render_data"]["label"]), MAX_LABEL_CHARS)
    check("正好 10 字符不动",
          button("一二三四五六七八九十", "x")["render_data"]["label"], "一二三四五六七八九十")
    check("英文 label 也按字符数截断",
          button("abcdefghijklmn", "x")["render_data"]["label"], "abcdefghij")

    # ---- 跳转按钮 ----
    lb = link_button("官网", "https://example.com")
    check("跳转按钮 type = 0", lb["action"]["type"], 0)
    check("跳转按钮 data = url", lb["action"]["data"], "https://example.com")
    check("跳转按钮不带 enter（那是指令按钮的字段）", "enter" in lb["action"], False)


# ==========================================================================
# B) keyboard 组装
# ==========================================================================

def part_b_keyboard() -> None:
    print("\n[B] keyboard()")
    kb = keyboard([button("A", "a")], [button("B", "b")])
    check("顶层只有 content", list(kb.keys()), ["content"])
    check("content 下只有 rows", list(kb["content"].keys()), ["rows"])
    check("两行", len(kb["content"]["rows"]), 2)
    check("每行键名是 buttons", list(kb["content"]["rows"][0].keys()), ["buttons"])
    check("第一行第一个按钮的 data", kb["content"]["rows"][0]["buttons"][0]["action"]["data"], "a")

    # ---- 行数上限 ----
    many = keyboard(*[[button(f"r{i}", "x")] for i in range(8)])
    check("行数截断到 5", len(many["content"]["rows"]), MAX_ROWS)

    # ---- 每行按钮上限 ----
    wide = keyboard([button(f"b{i}", "x") for i in range(9)])
    check("每行截断到 5 个",
          len(wide["content"]["rows"][0]["buttons"]), MAX_BUTTONS_PER_ROW)

    # ---- 空值处理 ----
    check("空行 / None 被跳过",
          len(keyboard([], [button("A", "a")], None)["content"]["rows"]), 1)
    check("完全没有按钮返回 None", keyboard(), None)
    check("只有空行也返回 None", keyboard([], None), None)
    check("空列表行也返回 None", keyboard([]), None)


# ==========================================================================
# B2) 内联指令（点歌名 → 填序号，不用按钮）
# ==========================================================================

def part_b2_inline_cmd() -> None:
    print("\n[B2] inline_cmd()")
    check(
        "默认是「参数指令」（enter=false，插入输入框）",
        inline_cmd("1. 晴天", "1"),
        "[1. 晴天](mqqapi://aio/inlinecmd?command=1&enter=false&reply=false)",
    )
    check_true("enter=True 时变成「回车指令」（群聊不支持，一般不用）",
               "&enter=true&" in inline_cmd("x", "1", enter=True))
    check(
        "command 做 url 编码（中文 + 空格都要转义）",
        inline_cmd("x", "点歌 晴天"),
        "[x](mqqapi://aio/inlinecmd?command=%E7%82%B9%E6%AD%8C%20%E6%99%B4%E5%A4%A9"
        "&enter=false&reply=false)",
    )
    check_true("链接文字就是用户看到的文本（可放歌名）",
               inline_cmd("7. 七里香 - 周杰伦", "7").startswith("[7. 七里香 - 周杰伦]("))


# ==========================================================================
# C) 平台能力表
# ==========================================================================

def part_c_caps() -> None:
    print("\n[C] 平台能力表里的 keyboard")
    from astrbot_plugin_rconsole.core.platform_caps import (
        _BUILTIN,
        _DEFAULT,
        OVERRIDABLE,
    )

    check_true("qqofficial 支持按钮", _BUILTIN["qqofficial"].keyboard is True)
    check_true("qqofficial_webhook 支持按钮",
               _BUILTIN["qqofficial_webhook"].keyboard is True)
    check_true("OneBot v11 不支持按钮", _BUILTIN["aiocqhttp"].keyboard is False)
    check_true("未知协议端保守取 False", _DEFAULT.keyboard is False)
    check_true("keyboard 可以被 platformProfiles 覆盖", "keyboard" in OVERRIDABLE)


# ==========================================================================
# D) 源码不变量
# ==========================================================================

def part_d_source() -> None:
    print("\n[D] 源码不变量")
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    const_src = (_ROOT / "core" / "constants.py").read_text(encoding="utf-8")

    # ---- 必须绕过适配器 ----
    check_true("有 _send_qq_payload（绕过适配器直发）",
               "async def _send_qq_payload" in main_src)
    check_true("走 botpy.http.Route", "from botpy.http import Route" in main_src)
    check_true("群聊路由 /v2/groups/{group_openid}/messages",
               "/v2/groups/{group_openid}/messages" in main_src)
    check_true("单聊路由 /v2/users/{openid}/messages",
               "/v2/users/{openid}/messages" in main_src)
    check_true("带 msg_seq（同一 msg_id 下必须互不相同）",
               'body["msg_seq"]' in main_src)

    # ---- 按钮必须和 markdown 同一条 ----
    check_true("_send_md_with_buttons 把 markdown 与 keyboard 放同一 payload",
               '"keyboard": kb' in main_src
               and '"markdown": {"content": markdown}' in main_src)

    # ---- 菜单：官机走纯 MD（不发图片）----
    check_true("有 _menu_markdown（官机 MD 菜单）", "def _menu_markdown" in main_src)
    check_true("官机菜单走 MD + 按钮", "self._menu_markdown(bot_name)" in main_src)
    check_true("MD 菜单失败会退回图片菜单",
               "MD 菜单发送失败，退回图片菜单" in main_src)
    check_true("有 qqButtons 总开关", "def _qq_buttons_enabled" in main_src)

    # ---- 点歌：MD 列表 + 按钮 / MD 详情 + 跳转按钮 + 独立语音 ----
    check_true("点歌列表有 MD 版（_send_music_list_md）",
               "async def _send_music_list_md" in main_src)
    check_true("官机点歌列表走 MD，且保留图片列表作退路",
               "await self._send_music_list_md(" in main_src
               and "render_song_list(" in main_src)
    check_true("点播详情是 MD（_send_music_detail_md）",
               "async def _send_music_detail_md" in main_src)
    check_true("「歌曲详情」是跳转按钮（type=0，不是指令按钮）",
               'qq_link_button("歌曲详情"' in main_src)
    # ---- 点歌列表改用「内联指令」（点歌名 → 填序号，不再挂底部按钮）----
    check_true("点歌列表用的是 core 的 inline_cmd（不是本地重实现）",
               "qq_inline_cmd(" in main_src
               and "def _qq_inline_cmd(" not in main_src)
    # 内联指令的格式由 [B2] 直接跑真函数验证，这里只盯「用了没」
    _list_seg = main_src.split("async def _send_music_list_md")[1].split(
        "async def _music_render_list_image"
    )[0]
    check_true("点歌列表不再挂底部按钮（改用内联指令）",
               "qq_button" not in _list_seg and "qq_keyboard" not in _list_seg)

    # ---- 官机语音：标准音质 + 放宽时长（适配器会转 silk）----
    check_true("官机点播取标准音质（high=False）",
               "self._music_resolve_url(song, high=False)" in main_src)
    check_true("有官机专用的语音时长上限",
               "_MUSIC_VOICE_MAX_SECONDS_QQ" in main_src)
    check_true("语音上限按协议端分流",
               'if caps.key == "qqofficial"' in main_src)
    check_true("官机点播 = MD 图文一条 + 语音一条",
               "md_ok = await self._send_music_detail_md(event, song, label)" in main_src
               and "preloaded=audio_path" in main_src)
    check_true("音频下载与发 MD 并行（不再串行白等）",
               "dl_task = asyncio.create_task(" in main_src
               and "audio_path = await dl_task" in main_src)
    check_true("失败提示按平台说清原因（QQ音乐＝登录态过期）",
               "def _music_fail_reason" in main_src
               and "12 小时有效期" in main_src)
    check_true("官机 card 自动降级为 voice",
               'if mode == "card" and not caps.music_card:' in main_src)

    # ---- 「图 + 按钮同一条」的两条候选路径 ----
    check_true("有 _upload_qq_image（上传拿 file_info）",
               "async def _upload_qq_image" in main_src)
    check_true("上传走官方富媒体 files 路由",
               "/v2/groups/{group_openid}/files" in main_src
               and "/v2/users/{openid}/files" in main_src)
    check_true("上传前 base64 编码", "base64.b64encode(png)" in main_src)
    check_true("上传用 srv_send_msg=False（只传不发）",
               '"srv_send_msg": False' in main_src)
    check_true("形态A：msg_type=7 + media + keyboard 挂同一条",
               '"msg_type": 7,' in main_src
               and '"media": {"file_info": file_info},' in main_src
               and '"keyboard": kb,' in main_src)
    check_true("形态A 补齐 content（适配器发富媒体时也会设它）",
               '"content": "",' in main_src)
    check_true("有对照组 _send_qq_image_only（只发图、不带按钮）",
               "async def _send_qq_image_only" in main_src)
    check_true("形态B：把 file_info 当 markdown 图片 URL",
               "async def _send_qq_embed_image_with_buttons" in main_src)
    check_true("有 mdImageWidth 取宽（缩放配置）", "def _md_image_width" in main_src)
    check_true(
        "_md_image 的宽度来自配置",
        "max_width=self._md_image_width(event)" in main_src,
    )
    check_true(
        "宽度按协议端读（profiles.<组>.md_image_width）",
        'conf_plat(event, "md_image_width"' in main_src,
    )

    # ---- 【关键坑】MD 图片必须带尺寸 ----
    check_true("_md_image 拼的是带尺寸语法（不带尺寸手机端只剩 [alt]）",
               "![{alt} #{w}px #{h}px]({url})" in main_src)
    check_true("_probe_image_size 用 Range 只取前 64KB",
               '"Range": "bytes=0-65535"' in main_src)

    # ---- 菜单按钮的设计意图 ----
    check_true("「点歌」按钮 enter=False（点击只填进输入框，等补参数）",
               'qq_button("点歌", "点歌 ", enter=False)' in main_src)
    check_true("菜单按钮含 R菜单 / 视频解析 / R配置",
               all(s in main_src for s in (
                   'qq_button("R菜单"', 'qq_button("视频解析"', 'qq_button("R配置"')))

    # ---- 顺手修的 bug ----
    check_true("_menu_text 签名带 event（否则内部用 event 会 NameError）",
               'self, bot_name: str = "", event: AstrMessageEvent | None = None'
               in main_src)

    # ---- 新命令注册 ----
    for key in ("buttons_test", "no_at", "resolve_help"):
        check_true(f"命令规则 {key} 已注册", f'"handler": "{key}"' in const_src)
        check_true(f"{key} 进了 _LOCAL_COMMAND_METHODS",
                   f'"{key}": "cmd_' in main_src)

    # ---- 专辑图 URL 规整（不规整的话手机上就是「图片加载失败」）----
    check_true("封面 URL 把 http 升成 https（QQ 只认 https）",
               'url.startswith("http://")' in main_src
               and '"https://" + url[len("http://"):]' in main_src)
    check_true("网易云封面加 ?param=WyH（原图最大近 1MB）",
               "param={size}y{size}" in main_src)
    check_true("QQ音乐封面换 R{size}x{size}M（原始只有 150×150）",
               "R{size}x{size}M" in main_src)
    check_true("专辑图走 _music_cover_url 而不是原始 cover",
               "cover = self._music_cover_url(song.cover)" in main_src)

    # ---- 免艾特只对官机说话（别的协议端没这个限制，说了会误导）----
    check_true("免艾特指引限定 qqofficial",
               'caps.key != "qqofficial"' in main_src)


def main() -> int:
    part_a_button()
    part_b_keyboard()
    part_b2_inline_cmd()
    part_c_caps()
    part_d_source()
    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：")
        for name in _FAILED:
            print("   -", name)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
