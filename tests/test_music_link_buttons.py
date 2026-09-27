#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点歌降级：把「纯文本链接」换成「带按钮的 markdown」（官机）。

**背景**（2026-09-27 线上日志）：

用户点歌只收到一坨纯文本 mp3 直链 ——

    [R插件][语音] 上传失败（25.5s），降级发链接
    http://m701.music.126.net/....mp3?vuutv=...

纯文本直链在 QQ 里又长又难点（手机上还会被折叠成「展开」）。
官机本来就有按钮能力（`#R菜单` 一直在用），降级路径却没接上。

锁住的东西：

- 四个降级分支（超时长 / 下载失败 / 上传超时 / 转码失败）都要先试按钮
- link 模式**单首**时也给按钮（列表 10 首塞不下按钮，保持文本）
- 按钮指向的是**音频直链**（能播完整），不是页面地址
- **按钮发不出去必须退回纯文本** —— 不能让用户什么都收不到
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

PASS = FAIL = 0


def check(name: str, got, expect=True, note: str = "") -> None:
    global PASS, FAIL
    ok = got == expect
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  期望={expect!r} 实际={got!r} {note}")


def check_true(name: str, got, note: str = "") -> None:
    check(name, bool(got), True, note)


def part_a_method() -> None:
    print("\n--- ① 新方法 ---")
    check_true("定义了 _music_link_with_buttons",
               "async def _music_link_with_buttons(" in MAIN)
    seg = MAIN.split("async def _music_link_with_buttons(")[1].split(
        "def _music_fail_hint(")[0]
    check_true("先判是不是官机（非官机直接 False）",
               "if not self._is_qq_official(event):" in seg)
    check_true("再判按钮开没开", "if not self._qq_buttons_enabled(event):" in seg)
    check_true("用 link_button 拼跳转按钮", "qq_link_button(" in seg)
    check_true("先放「播放完整音频」的音频直链", '"▶ 播放完整音频"' in seg)
    check_true("再放「歌曲详情」的页面地址", '"歌曲详情"' in seg)
    check_true("两个地址都空时返回 False", "if not rows:" in seg)
    check_true("发的是「MD + 按钮」", "_send_md_with_buttons(" in seg)
    check_true("markdown 里带歌名", "🎵 {song.name}" in seg)


def part_b_callers() -> None:
    print("\n--- ② 降级分支都接了 ---")
    n = MAIN.count("await self._music_link_with_buttons(")
    # 6 个降级分支（超时长 / 下载失败×2 / 上传超时 / 转码失败 / 卡片构造失败）
    # + link 模式单首 = 7 处
    check("调用点 7 处", n, 7)

    for tag, kw in (
        ("超时长分支", "约 {song.duration // 60} 分"),
        ("下载失败分支", '"音频下载失败"'),
        ("上传超时分支", '"语音上传超时（QQ 接口不稳）"'),
        ("转码失败分支", '"语音转码失败（音频可能过长）"'),
        ("卡片构造失败", '"卡片构造失败"'),
    ):
        check_true(f"{tag} 接了按钮", kw in MAIN)

    check_true("link 模式：只在单首时给按钮",
               "if len(songs) == 1 and await self._music_link_with_buttons(" in MAIN)


def part_c_fallback() -> None:
    print("\n--- ③ 纯文本兜底必须保留 ---")
    check_true("超时长的纯文本分支还在",
               'f"🎵「{song.label}」{headline}。\\n"' in MAIN)
    check_true("下载失败的纯文本分支还在（至少 2 处：语音路 + MD 详情路）",
               MAIN.count('音频下载失败，改用链接') >= 2)
    check_true("上传超时的纯文本分支还在",
               'f"🎵「{song.label}」语音上传超时（QQ 接口不稳），先给链接：\\n"' in MAIN)
    check_true("转码失败的纯文本分支还在",
               'f"🎵「{song.label}」语音转码失败（音频可能过长），改用链接：\\n"' in MAIN)
    check_true("卡片构造失败的纯文本分支还在",
               'f"🎵 卡片构造失败，先给链接：\\n{self._music_link(song)}"' in MAIN)

    # 每个按钮调用之后都要有纯文本兜底（成对出现，别把兜底删了）
    idxs, start = [], 0
    while True:
        i = MAIN.find("await self._music_link_with_buttons(", start)
        if i < 0:
            break
        idxs.append(i)
        start = i + 1
    check("按钮调用扫描到 7 处", len(idxs), 7)
    with_fallback = sum(1 for i in idxs if "plain_result(" in MAIN[i:i + 900])
    # link 单首那处的兜底在 for 循环之后，窗口里可能看不到
    check_true(f"紧跟纯文本兜底的有 {with_fallback} 处（应 ≥6）", with_fallback >= 6)


def part_d_control() -> None:
    print("\n--- ④ 开关与既有能力 ---")
    check_true("_qq_buttons_enabled 存在", "def _qq_buttons_enabled(" in MAIN)
    check_true("_send_md_with_buttons 存在", "async def _send_md_with_buttons(" in MAIN)
    seg = MAIN.split("async def _send_md_with_buttons(")[1].split(
        "async def _upload_qq_media(")[0]
    check_true("_send_md_with_buttons 内部走 _send_qq_payload",
               "_send_qq_payload(" in seg)
    check_true("按钮拼不出来时返回 False（可降级）",
               "if kb is None:" in seg and "return False" in seg)
    check_true("卡片那条路的「歌曲详情/保存音频」按钮没被动过",
               'qq_link_button("歌曲详情", song.page_url' in MAIN
               and 'qq_link_button("保存音频", play' in MAIN)


if __name__ == "__main__":
    part_a_method()
    part_b_callers()
    part_c_fallback()
    part_d_control()
    print()
    print(f"===== {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)
