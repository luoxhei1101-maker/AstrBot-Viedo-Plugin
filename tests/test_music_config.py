#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3.0 配置重组测试：点歌配置从 netease/other 搬进独立的 music 分组。

这个迁移**必须**做对，否则用户辛苦抓的 Cookie 会在升级时丢掉。
覆盖的边界：

- 正常搬运（旧值非空、新位置还是默认值）
- **不该覆盖**：用户已经在新位置设过值
- **旧值本身就是默认值**时不做无意义的写盘
- **废弃键**要清掉，旧分组清空后整组删除
- 旧平台值不再支持时回退（``kugou`` -> ``netease``）
- 全新安装不产生任何噪音
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _L:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def debug(self, *a, **k): pass

    _api.logger = _L()
    _a.api = _api
    sys.modules["astrbot"] = _a
    sys.modules["astrbot.api"] = _api

from astrbot_plugin_rconsole.core.config_migrate import migrate_music_config  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {msg}")
    else:
        FAIL += 1
        print(f"[FAIL] {msg}")


def fresh_music(**over):
    """AstrBot 用 schema 默认值补齐后的 music 分组。"""
    base = {
        "enable": False,
        "platform": "netease",
        "maxList": 10,
        "sendMode": "link",
        "neteaseCookie": "",
        "qqMusicCookie": "",
    }
    base.update(over)
    return base


def test_full_migration() -> None:
    print("\n--- 老用户全量迁移 ---")
    conf = {
        "plugin": {"enable": True},
        "netease": {
            "isSendVocal": True,
            "useNeteaseSongRequest": True,
            "songRequestPlatform": "qq",
            "songRequestMaxList": 15,
            "useLocalNeteaseAPI": True,
            "neteaseCloudAPIServer": "http://x",
            "neteaseCookie": "MUSIC_U=ABC",
            "neteaseCloudCookie": "MUSIC_U=CLOUD",
            "neteaseCloudAudioQuality": "lossless",
        },
        "other": {
            "weiboCookie": "keep-me",
            "kugouApiServer": "http://k",
            "kugouAudioQuality": "128k",
            "kugouCookie": "token=x",
            "kugouCookieFields": [],
            "qqMusicCookie": "qqmusic_key=QQ",
            "qqMusicAudioQuality": "320k",
        },
        "music": fresh_music(),
    }
    changes = migrate_music_config(conf)
    for c in changes:
        print(f"      · {c}")

    m = conf["music"]
    check(m["enable"] is True, "enable: netease.useNeteaseSongRequest -> music.enable")
    check(m["platform"] == "qq", "platform 搬运正确")
    check(m["maxList"] == 15, "maxList 搬运正确")
    check(m["neteaseCookie"] == "MUSIC_U=ABC", "neteaseCookie 搬到位")
    check(m["qqMusicCookie"] == "qqmusic_key=QQ", "qqMusicCookie 搬到位")
    check("netease" not in conf, "netease 组清空后整组删除")
    check(conf["other"]["weiboCookie"] == "keep-me", "无关键未被误删")
    for k in ("kugouApiServer", "kugouAudioQuality", "kugouCookie",
              "kugouCookieFields", "qqMusicAudioQuality"):
        check(k not in conf["other"], f"废弃键 other.{k} 已清理")


def test_no_overwrite() -> None:
    print("\n--- 新位置已有用户设置：不得覆盖 ---")
    conf = {
        "netease": {
            "useNeteaseSongRequest": True,
            "songRequestPlatform": "netease",
            "songRequestMaxList": 15,
            "neteaseCookie": "OLD",
        },
        "other": {"qqMusicCookie": "OLDQQ"},
        "music": fresh_music(maxList=20, sendMode="card",
                             neteaseCookie="NEW", qqMusicCookie="NEWQQ"),
    }
    migrate_music_config(conf)
    m = conf["music"]
    check(m["maxList"] == 20, "maxList 保留新值 20（旧值 15 被丢弃）")
    check(m["neteaseCookie"] == "NEW", "neteaseCookie 保留新值")
    check(m["qqMusicCookie"] == "NEWQQ", "qqMusicCookie 保留新值")
    check(m["enable"] is True, "enable 仍在默认值 -> 采用旧值 True")
    check(m["sendMode"] == "card", "sendMode 不受迁移影响")


def test_default_noop() -> None:
    print("\n--- 旧值等于默认值：不产生写盘 ---")
    conf = {
        "netease": {"useNeteaseSongRequest": False, "songRequestMaxList": 10},
        "music": fresh_music(),
    }
    changes = migrate_music_config(conf)
    check("music.enable" not in " ".join(changes), "enable 默认值 False 不搬")
    check("music.maxList" not in " ".join(changes), "maxList 默认值 10 不搬")


def test_platform_fallback() -> None:
    print("\n--- 旧平台值已不支持：回退 ---")
    conf = {"netease": {"songRequestPlatform": "kugou"}, "music": fresh_music()}
    migrate_music_config(conf)
    check(conf["music"]["platform"] == "netease", "kugou -> netease 回退")

    conf2 = {"netease": {"songRequestPlatform": "qqmusic"}, "music": fresh_music()}
    migrate_music_config(conf2)
    check(conf2["music"]["platform"] == "qq", "qqmusic -> qq 归一化")


def test_new_install() -> None:
    print("\n--- 全新安装：零动作 ---")
    conf = {"music": fresh_music()}
    changes = migrate_music_config(conf)
    check(changes == [], f"无迁移记录（实际 {changes}）")


def test_empty_values() -> None:
    print("\n--- 空值：不搬，但键要清 ---")
    conf = {
        "netease": {"neteaseCookie": "", "songRequestMaxList": 0, "isSendVocal": False},
        "music": fresh_music(),
    }
    migrate_music_config(conf)
    check(conf["music"]["neteaseCookie"] == "", "空 Cookie 不搬")
    check(conf["music"]["maxList"] == 10, "maxList 保持默认")
    check("netease" not in conf, "netease 组仍被清空删除")


def test_idempotent() -> None:
    print("\n--- 幂等：跑两次结果一致（插件加载会多次触发）---")
    conf = {
        "netease": {"useNeteaseSongRequest": True, "neteaseCookie": "CK",
                    "songRequestMaxList": 15, "songRequestPlatform": "qq"},
        "other": {"qqMusicCookie": "Q", "kugouCookie": "x"},
        "music": fresh_music(),
    }
    migrate_music_config(conf)
    snapshot = {k: dict(v) if isinstance(v, dict) else v for k, v in conf.items()}
    second = migrate_music_config(conf)
    check(second == [], f"第二次无动作（实际 {second}）")
    check(conf == snapshot, "第二次运行配置未被改动")


def main() -> int:
    test_full_migration()
    test_no_overwrite()
    test_default_noop()
    test_platform_fallback()
    test_new_install()
    test_empty_values()
    test_idempotent()
    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("❌ 有失败项")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
