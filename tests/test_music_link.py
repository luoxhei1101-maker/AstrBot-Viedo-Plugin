"""点歌降级提示里的链接：**优先音频直链**，而不是歌曲详情页。

**用户要求（2026-09-23）**：语音 / 文件那几条路失败时给出的链接，应该是
「文件直链」—— 就是**语音发送用的那条** ``song.play_url`` —— 而不是
``song.page_url`` 详情页。理由：直链在客户端能**直接点开播放**，
详情页还要再跳一次。

要守住的不变量：

1. ``_music_link()`` 优先 ``play_url``、没有才退回 ``page_url``；
2. 所有「降级给链接」的提示都用它（8 处）；
3. ``_music_render_link``（link 模式）和「歌曲详情」跳转按钮**仍然给页面**
   —— 那两个的语义本来就是「给页面」。

跑法::

    python tests/test_music_link.py
"""

from __future__ import annotations

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


class _Song:
    def __init__(self, play_url: str = "", page_url: str = "https://page/x"):
        self.play_url = play_url
        self.page_url = page_url


# ==========================================================================
# A) _music_link 行为
# ==========================================================================

def part_a_behavior() -> None:
    print("\n[A] _music_link 取值")
    import astrbot_plugin_rconsole.main as main_mod

    link = main_mod.Main._music_link

    check(
        "有直链 -> 给直链（语音用的那条）",
        link(_Song("https://cdn/a.mp3")),
        "https://cdn/a.mp3",
    )
    check(
        "没直链 -> 退回详情页",
        link(_Song("")),
        "https://page/x",
    )
    check(
        "直链是空白串 -> 也算没直链",
        link(_Song("   ")),
        "https://page/x",
    )
    check("两边都空 -> 空串", link(_Song("", "")), "")

    # 属性缺失不该炸（历史缓存里的老对象可能没有 play_url）
    class _Old:
        page_url = "https://page/y"

    check("缺 play_url 属性也不炸", link(_Old()), "https://page/y")


# ==========================================================================
# B) 源码不变量
# ==========================================================================

def part_b_source() -> None:
    print("\n[B] 源码不变量")
    src = (_ROOT / "main.py").read_text(encoding="utf-8")

    check_true("有 _music_link", "def _music_link(song) -> str:" in src)
    n = src.count("self._music_link(")
    check_true(
        f"8 处降级提示都改用它（实际 {n} 处）",
        n == 8,
        "少一处就说明还有地方在给详情页",
    )

    # 该保留 page_url 的两处
    check_true(
        "link 模式仍然列详情页（它的语义就是给页面）",
        "lines.append(f\"   {song.page_url}\")" in src,
    )
    check_true(
        "「歌曲详情」按钮仍然是详情页（永久入口，直链失效了还能找）",
        'qq_link_button("歌曲详情", song.page_url' in src,
    )
    check_true(
        "新增「保存音频」按钮指向音频直链 —— **点开能保存**（用户要求）",
        'qq_link_button("保存音频", play, style=1)' in src,
    )
    check_true(
        "两个按钮都给：一个能保存、一个不会过期",
        "buttons.append([qq_link_button(\"歌曲详情\"" in src
        and "buttons.append([qq_link_button(\"保存音频\"" in src,
    )
    check_true(
        "直链按钮只在 play_url 非空时加（取不到就不给死链）",
        'play = str(getattr(song, "play_url", "") or "").strip()' in src
        and "if play:" in src,
    )
    check_true(
        "卡片的 url / audio 字段没被动（那是卡片自己的字段）",
        "audio=song.play_url or song.page_url" in src,
    )


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 点歌降级链接回归测试")
    print("=" * 70)
    part_a_behavior()
    part_b_source()

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
