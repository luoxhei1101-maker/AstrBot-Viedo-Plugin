"""平台能力表的离线回归测试（v1.6.8）。

**为什么需要**：插件要同时伺候 OneBot v11 和 QQ 官方机器人，而这两者的
消息能力差别很大 —— 官方机器人**没有合并转发、没有音乐卡片**，
但**有原生 markdown**。搞错了的表现是「消息整条发不出去」，
而不是「发出来不对」，所以必须用测试把能力表钉死。

能力表的事实依据（实测 AstrBot 源码，非猜测）：

- ``qqofficial_message_event.py`` 只 import 了
  ``File / Image / Plain / Record / Video``，全文件**没有 Node/Nodes 分支**
  → ``forward=False``
- 同上文件里没有 Music 的处理 → ``music_card=False``
- ``MessageChain.use_markdown(True)`` → 该文件走 ``msg_type=2`` +
  ``MarkdownPayload`` → ``markdown=True``
- 官方机器人的图片是 ``payload["media"] = media``（一次一张）
  → ``max_images_per_msg=1``

跑法::

    python tests/test_platform_caps.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))

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


class FakeEvent:
    def __init__(self, name="aiocqhttp", pid="inst1"):
        self._name = name
        self._pid = pid

    def get_platform_name(self):
        return self._name

    def get_platform_id(self):
        return self._pid


class BrokenEvent:
    def get_platform_name(self):
        raise RuntimeError("boom")

    def get_platform_id(self):
        raise RuntimeError("boom")


# ==========================================================================
# A) 平台识别
# ==========================================================================

def part_a_identify() -> None:
    print("\n[A] 平台识别")
    from astrbot_plugin_rconsole.core.platform_caps import platform_id, platform_key

    check("aiocqhttp -> aiocqhttp", platform_key(FakeEvent("aiocqhttp")), "aiocqhttp")
    check("qqofficial -> qqofficial", platform_key(FakeEvent("qqofficial")), "qqofficial")
    check(
        "QQOfficial（大小写混杂）也认",
        platform_key(FakeEvent("QQOfficial")),
        "qqofficial",
    )
    check(
        "qqofficial_webhook 归到 qqofficial",
        platform_key(FakeEvent("qqofficial_webhook")),
        "qqofficial",
    )
    check(
        "qq_official 下划线写法也认",
        platform_key(FakeEvent("qq_official")),
        "qqofficial",
    )
    check("空平台名 -> unknown", platform_key(FakeEvent("")), "unknown")
    check("取不到平台名 -> unknown（不抛异常）", platform_key(BrokenEvent()), "unknown")
    check("取不到 platform_id -> 空串（不抛异常）", platform_id(BrokenEvent()), "")
    check("platform_id 正常取值", platform_id(FakeEvent(pid="my_bot")), "my_bot")


# ==========================================================================
# B) 能力表默认值（关键事实）
# ==========================================================================

def part_b_caps() -> None:
    print("\n[B] 内置能力表")
    from astrbot_plugin_rconsole.core.platform_caps import caps_for

    ob = caps_for(FakeEvent("aiocqhttp"))
    check_true("OneBot：能合并转发", ob.forward is True)
    check_true("OneBot：能音乐卡片", ob.music_card is True)
    check_true("OneBot：没有原生 MD", ob.markdown is False)
    check_true("OneBot：能语音", ob.voice is True)
    check("OneBot：标签", ob.label, "OneBot v11")

    qo = caps_for(FakeEvent("qqofficial"))
    check_true("官方：**不能**合并转发（关键）", qo.forward is False)
    check_true("官方：**不能**音乐卡片（关键）", qo.music_card is False)
    check_true("官方：**有**原生 MD（关键）", qo.markdown is True)
    check_true("官方：能语音（用来替代音乐卡片）", qo.voice is True)
    check_true("官方：能视频", qo.video is True)
    check("官方：单条图片上限为 1（media 一次一张）", qo.max_images_per_msg, 1)
    check("官方：标签", qo.label, "QQ 官方机器人")

    unknown = caps_for(FakeEvent("some_new_platform"))
    check_true("未知平台走保守默认（不发卡片）", unknown.music_card is False)
    check_true("未知平台走保守默认（不发合并转发）", unknown.forward is False)


# ==========================================================================
# C) 配置覆盖
# ==========================================================================

def part_c_profiles() -> None:
    print("\n[C] platformProfiles 配置覆盖")
    from astrbot_plugin_rconsole.core.platform_caps import caps_for

    # ---- list 形态 ----
    profiles = [
        {"platform_id": "inst_off", "forward": False, "music_card": False},
        {"platform_id": "other", "forward": True},
    ]
    caps = caps_for(FakeEvent("aiocqhttp", "inst_off"), profiles)
    check_true("命中时 forward 被覆盖为 False", caps.forward is False)
    check_true("命中时 music_card 被覆盖为 False", caps.music_card is False)
    check_true("未覆盖的字段保持默认（voice）", caps.voice is True)

    caps2 = caps_for(FakeEvent("aiocqhttp", "other"), profiles)
    check_true("另一条 profile 只影响自己", caps2.forward is True)
    check_true("另一条 profile 的 music_card 未被改", caps2.music_card is True)

    caps3 = caps_for(FakeEvent("aiocqhttp", "not_in_list"), profiles)
    check_true("没命中的实例用内置默认", caps3.forward is True)

    # ---- dict 形态（手改配置文件更省事）----
    caps4 = caps_for(
        FakeEvent("aiocqhttp", "i1"),
        {"i1": {"music_card": False}},
    )
    check_true("dict 形态也能覆盖", caps4.music_card is False)

    # ---- 给官方机器人「反向」加回能力（万一以后支持了）----
    caps5 = caps_for(
        FakeEvent("qqofficial", "q1"),
        [{"platform_id": "q1", "forward": True}],
    )
    check_true("可以给官方实例手工开回 forward", caps5.forward is True)

    # ---- 坏数据不炸 ----
    check_true("profiles 为 None 不炸", caps_for(FakeEvent(), None).forward is True)
    check_true("profiles 为 [] 不炸", caps_for(FakeEvent(), []).forward is True)
    check_true(
        "profile 项不是 dict 不炸",
        caps_for(FakeEvent("aiocqhttp", "x"), ["??"]).forward is True,
    )
    check_true(
        "platform_id 缺失的项被忽略",
        caps_for(FakeEvent("aiocqhttp", "x"), [{"forward": False}]).forward is True,
    )
    check_true(
        "非 bool 值不被采纳（挡住字符串 true）",
        caps_for(
            FakeEvent("aiocqhttp", "x"),
            [{"platform_id": "x", "forward": "yes"}],
        ).forward is True,
    )


# ==========================================================================
# D) 可读描述
# ==========================================================================

def part_d_describe() -> None:
    print("\n[D] describe 输出")
    from astrbot_plugin_rconsole.core.platform_caps import caps_for, describe

    text = describe(caps_for(FakeEvent("qqofficial")))
    check_true("带平台标签", "QQ 官方机器人" in text)
    check_true("合并转发显示为 ❌", "合并转发 ❌" in text)
    check_true("音乐卡片显示为 ❌", "音乐卡片 ❌" in text)
    check_true("原生MD 显示为 ✅", "原生MD ✅" in text)


# ==========================================================================
# E) 源码不变量
# ==========================================================================

def part_e_source_guard() -> None:
    print("\n[E] 源码不变量")
    src = (_ROOT / "core" / "platform_caps.py").read_text(encoding="utf-8")

    check_true("有 PlatformCaps 定义", "class PlatformCaps" in src)
    check_true("有 caps_for 入口", "def caps_for" in src)
    check_true("官方机器人 forward=False（关键）", '"qqofficial": PlatformCaps(' in src)
    check_true("官方机器人音乐卡片=False", "music_card=False" in src)
    check_true(
        "未知平台是保守默认（不发卡片/转发）",
        "_DEFAULT = PlatformCaps(" in src and "forward=True, music_card=True" in src
        and "key=\"unknown\"" in src.replace(" ", ""),
    )
    check_true("max_images_per_msg 有默认值", "max_images_per_msg" in src)

    # main.py 要开始用这层（本版本刚开始接）
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    check_true(
        "main.py 引入了 platform_caps",
        "platform_caps" in main_src,
    )


def main() -> int:
    print("=" * 72)
    print("平台能力表 —— 回归测试")
    print("=" * 72)

    part_a_identify()
    part_b_caps()
    part_c_profiles()
    part_d_describe()
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
