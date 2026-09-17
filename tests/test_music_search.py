#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点歌模块离线测试（不联网、不需要 Cookie）。

覆盖的是**纯逻辑**部分——这些是踩过坑的地方，改动时最容易回归：

- Cookie 字段别名映射（QQ 音乐后台的 Cookie 名与接口参数名不一致）
- 音质档位候选构造（``filename`` 拼错就会静默降级到最低音质）
- 音频格式识别（mp3 / m4a / flac / ogg，误判会让"下载成功"变成假象）
- 限流判定（2001 是随机拒绝码，漏判会把空结果当成功）
- 命令正则（漏了会让整条链路不触发）

联网行为（真实搜索 / 取直链）不在这个文件里——那需要会员 Cookie 和
网络，见仓库外的验证脚本。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import types  # noqa: E402

# AstrBot 运行环境桩：本模块只用到 logger
if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    _api.logger = _Logger()
    _a.api = _api
    sys.modules["astrbot"] = _a
    sys.modules["astrbot.api"] = _api

from astrbot_plugin_rconsole.core import music_search as ms  # noqa: E402
from astrbot_plugin_rconsole.core.constants import COMMAND_RULES  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {msg}")
    else:
        FAIL += 1
        print(f"[FAIL] {msg}")


# ==========================================================================
# 1. Cookie 字段别名映射
# ==========================================================================


def cookie_check() -> None:
    print("\n--- Cookie 解析与别名映射 ---")

    # 用户从 y.qq.com 抓的真实字段名（实测确认的映射关系）
    real = (
        "uid=2593504303; qqopenid=OPEN1; qqyunionid=UNION1; "
        "qqaccess_token=ATOK1; psrf_qqrefresh_token=RT1; qm_keyst=KEY1"
    )
    m = ms.parse_cookie(real)
    check(m.get("uin") == "2593504303", "uid -> uin")
    check(m.get("psrf_qqopenid") == "OPEN1", "qqopenid -> psrf_qqopenid")
    check(m.get("psrf_qqunionid") == "UNION1", "qqyunionid -> psrf_qqunionid")
    check(m.get("psrf_qqaccess_token") == "ATOK1", "qqaccess_token -> psrf_qqaccess_token")
    check(m.get("qqmusic_key") == "KEY1", "qm_keyst -> qqmusic_key")

    # 已经用了标准名的情况：不该被别名覆盖
    std = ms.parse_cookie("uin=999; qqmusic_key=STDKEY")
    check(std.get("uin") == "999", "标准名 uin 不被覆盖")
    check(std.get("qqmusic_key") == "STDKEY", "标准名 qqmusic_key 不被覆盖")

    # 值里含 = 必须完整保留（base64 常见）
    eq = ms.parse_cookie("qm_keyst=AbC==dEf==; uin=1")
    check(eq.get("qqmusic_key") == "AbC==dEf==", "值里的 = 完整保留")

    # 空串 / 脏输入不该炸
    check(ms.parse_cookie("") == {}, "空串 -> 空 dict")
    check("" not in ms.parse_cookie("; ; =; a=b"), "空 key 被跳过")

    # 换行分隔也能解析（用户从某些地方复制会带换行）
    nl = ms.parse_cookie("uin=7\nqm_keyst=K")
    check(nl.get("uin") == "7" and nl.get("qqmusic_key") == "K", "换行分隔可解析")


# ==========================================================================
# 2. 音质档位候选
# ==========================================================================


def quality_check() -> None:
    print("\n--- 音质档位 filename 构造 ---")

    # 实测拿到的 file 结构（周杰伦《晴天》）
    file_info = {
        "media_mid": "003Qui1q2u1Zho",
        "size_320mp3": 10792943,
        "size_128mp3": 4317292,
        "size_96aac": 3283546,
        "size_192ogg": 0,  # 这首没有 192ogg
    }
    names = ms._qq_filenames(file_info, "003Qui1q2u1Zho", high=True)
    check(names[0] == "M800003Qui1q2u1Zho.mp3", "首选 320kbps mp3（M800）")
    check("O600003Qui1q2u1Zho.ogg" not in names, "size 为 0 的档位被跳过")
    check("M500003Qui1q2u1Zho.mp3" in names, "次选 128kbps（M500）")
    check("C400003Qui1q2u1Zho.m4a" in names, "兜底 96kbps（C400）")

    # 低音质模式只给 128k
    low = ms._qq_filenames(file_info, "003Qui1q2u1Zho", high=False)
    check(low == ["M500003Qui1q2u1Zho.mp3"], f"high=False 只给 128k: {low}")

    # media_mid 缺失时不能拼出垃圾文件名
    check(ms._qq_filenames(file_info, "", high=True) == [], "无 media_mid -> 空列表")

    # file 全空时也不该崩
    check(ms._qq_filenames({}, "MID", high=True) == [], "file 为空 -> 空列表")

    # 只有 96aac 的歌
    only_aac = {"size_96aac": 100}
    got = ms._qq_filenames(only_aac, "MID", high=True)
    check(got == ["C400MID.m4a"], f"只有 96aac 的歌: {got}")


# ==========================================================================
# 3. 音频格式识别
# ==========================================================================


def audio_check() -> None:
    print("\n--- 音频格式识别 ---")

    pad = b"\x00" * 200
    check(ms.looks_like_audio(b"ID3\x03\x00\x00\x00" + pad), "mp3（ID3 标签）")
    check(ms.looks_like_audio(b"\xff\xfb\x90\x00" + pad), "mp3（MPEG 同步字）")
    # m4a 的特征是第 4-8 字节为 ftyp（前 4 字节是 box size）
    check(ms.looks_like_audio(b"\x00\x00\x00\x20ftypmp42" + pad), "m4a（ftyp box）")
    check(ms.looks_like_audio(b"fLaC\x00\x00\x00\x22" + pad), "flac")
    check(ms.looks_like_audio(b"OggS\x00\x02" + pad), "ogg")

    # 反例：不能把错误页/JSON 当音频
    check(not ms.looks_like_audio(b'{"code":404,"msg":"not found"}' + pad), "JSON 不是音频")
    check(not ms.looks_like_audio(b"<html><body>403 Forbidden" + pad), "HTML 不是音频")
    check(not ms.looks_like_audio(b"short"), "过短 -> False")
    check(not ms.looks_like_audio(b""), "空 -> False")


# ==========================================================================
# 4. 限流判定
# ==========================================================================


def rate_limit_check() -> None:
    print("\n--- 限流判定 ---")

    # 实测的限流响应：search.code == 2001 且 body 空
    limited = {"code": 0, "search": {"code": 2001, "data": {"body": {}}}}
    check(ms._qqm_rate_limited(limited), "search.code=2001 判定为限流")

    # 取直链接口的限流形态
    limited2 = {"code": 0, "req_0": {"code": 2001}}
    check(ms._qqm_rate_limited(limited2), "req_0.code=2001 判定为限流")

    # 正常响应不能被误判
    normal = {
        "code": 0,
        "search": {"code": 0, "data": {"body": {"item_song": [{"mid": "x"}]}}},
    }
    check(not ms._qqm_rate_limited(normal), "正常响应不误判")

    # req_0.code=0 但 purl 为空（musickey 过期）——不该当成限流
    expired = {"req_0": {"code": 0, "data": {"midurlinfo": [{"purl": "", "result": 104003}]}}}
    check(not ms._qqm_rate_limited(expired), "musickey 过期（104003）不算限流")


# ==========================================================================
# 5. 命令正则
# ==========================================================================


def regex_check() -> None:
    print("\n--- 命令正则 ---")

    rule = next((r for r in COMMAND_RULES if r["handler"] == "music_search"), None)
    check(rule is not None, "COMMAND_RULES 里注册了 music_search")
    if not rule:
        return

    check(rule["admin"] is False, "点歌不需要管理员权限")

    pat = ms_pattern()
    # (输入, 期望的平台 key 或 None, 期望的关键词)
    cases: list[tuple[str, str | None, str | None]] = [
        # 不带平台 -> 用配置默认（platform 为 None）
        ("点歌 晴天", None, "晴天"),
        ("点歌晴天", None, "晴天"),
        ("#点歌 晴天", None, "晴天"),
        ("/点歌 晴天", None, "晴天"),
        # 平台写在「点歌」前面（本次新增，主推用法）
        ("网易云点歌 晴天", "netease", "晴天"),
        ("网易点歌 稻香", "netease", "稻香"),
        ("网抑云点歌 稻香", "netease", "稻香"),
        ("QQ点歌 晴天", "qqmusic", "晴天"),
        ("qq点歌 晴天", "qqmusic", "晴天"),
        ("Qq点歌 晴天", "qqmusic", "晴天"),      # 大小写混写
        ("QQ音乐点歌 稻香", "qqmusic", "稻香"),
        ("网易云 点歌 晴天", "netease", "晴天"),   # 平台和「点歌」之间有空格
        # 平台写在后面（向后兼容早期写法）
        ("#点歌 网易云 晴天", "netease", "晴天"),
        ("#点歌 QQ音乐 晴天", "qqmusic", "晴天"),
    ]
    for text, want_plat, want_kw in cases:
        m = re.search(pat, text, re.I | re.M)
        if not m:
            check(False, f"命中 {text!r}")
            continue
        pre = (m.group("pre") or "").strip()
        post = (m.group("post") or "").strip()
        raw = (pre or post).lower()
        got_plat = {"网易云": "netease", "网抑云": "netease", "网易": "netease",
                    "qq音乐": "qqmusic", "qq": "qqmusic"}.get(raw)
        got_kw = (m.group("kw") or "").strip()
        check(
            got_plat == want_plat and got_kw == want_kw,
            f"{text!r} -> 平台={got_plat} 关键词={got_kw!r}（期望 {want_plat} / {want_kw!r}）",
        )

    # 不该命中的
    for text in ("点歌", "随便说点什么", "", "我今天点歌"):  # 「我今天点歌」不以點歌开头
        check(
            not re.search(pat, text, re.I | re.M),
            f"不命中 {text!r}",
        )


def ms_pattern() -> str:
    """取点歌命令的正则（与 COMMAND_RULES 共用同一份）。"""
    from astrbot_plugin_rconsole.core.constants import MUSIC_COMMAND_PATTERN

    rule = next(r for r in COMMAND_RULES if r["handler"] == "music_search")
    check(rule["pattern"] == MUSIC_COMMAND_PATTERN,
          "COMMAND_RULES 与 main.py 共用同一份正则（未各写一份）")
    return MUSIC_COMMAND_PATTERN


# ==========================================================================
# 6. 平台映射
# ==========================================================================


def platform_check() -> None:
    print("\n--- 平台映射 ---")

    check(ms.PLATFORM_ALIASES.get("网易云") == "netease", "别名 网易云 -> netease")
    check(ms.PLATFORM_ALIASES.get("QQ") == "qqmusic", "别名 QQ -> qqmusic")
    check(ms.PLATFORM_LABELS.get("netease") == "网易云音乐", "展示名 netease")
    check(ms.PLATFORM_LABELS.get("qqmusic") == "QQ音乐", "展示名 qqmusic")

    # 默认顺序：网易云优先（匿名即可取直链，稳定性更好）
    check(ms.DEFAULT_ORDER[0] == "netease", "DEFAULT_ORDER 首选网易云")

    # 酷狗已移除
    check("kugou" not in ms.PLATFORM_LABELS, "不含酷狗")
    check("kugou" not in ms.PLATFORM_ALIASES.values(), "别名里也没有酷狗")


# ==========================================================================
# 7. QQ 音乐请求头（踩过的坑，必须锁住）
# ==========================================================================


def header_check() -> None:
    print("\n--- QQ 音乐请求头约束 ---")

    # 实测：带 Accept-Language 会固定触发 search.code=2001 空结果
    check(
        "Accept-Language" not in ms._QQM_HEADERS,
        "QQ 音乐请求头**不含** Accept-Language（否则接口返 2001 空结果）",
    )
    check("User-Agent" in ms._QQM_HEADERS, "带 UA")
    check("Content-Type" in ms._QQM_HEADERS, "带 Content-Type")

    # 缓存与重试参数要合理
    check(ms._QQM_RETRIES >= 2, f"重试次数 >= 2（实测 {ms._QQM_RETRIES}）")
    check(ms._SEARCH_CACHE_TTL >= 300, f"搜索缓存 TTL >= 5 分钟（实测 {ms._SEARCH_CACHE_TTL}s）")


def main() -> int:
    cookie_check()
    quality_check()
    audio_check()
    rate_limit_check()
    regex_check()
    platform_check()
    header_check()

    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("❌ 有失败项")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
