# -*- coding: utf-8 -*-
"""小红书 resolver 的离线测试。

重点锁两件事：

1. **Cookie 必须剔除 ``webId``** —— 带上它会稳定拿到验证页而不是内容页
   （实测对照见 ``platforms/xiaohongshu.py`` 的模块 docstring）。
   这条一旦被「优化」掉，功能会整体失效且很难查。
2. **``__INITIAL_STATE__`` 的解析与取值** —— ``undefined`` 必须替换成
   ``null``，话题标签 ``#x[话题]#`` 要规整。

全部离线，不依赖网络与 Cookie。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))

if "astrbot" not in sys.modules:
    _api = types.ModuleType("astrbot.api")
    _api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api.AstrBotConfig = dict
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

from astrbot_plugin_rconsole.platforms import xiaohongshu as xhs  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, got, want=True) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        print(f"  [!! ] {name}\n        got ={got!r}\n        want={want!r}")


def check_in(name: str, needle, haystack) -> None:
    global PASS, FAIL
    if needle in haystack:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        print(f"  [!! ] {name}: {needle!r} 不在 {str(haystack)[:120]!r}")


# 一份「像真实抓包」的整串 Cookie（值都是假的，只保留结构）
FULL = (
    "acw_tc=abc; abRequestId=01e1ad09; ets=1789708412535; webBuild=6.53.3; "
    "a1=1a0b2ef26f2j2govwvp0a5r8h4dnj3maxygd6knco50000394136; "
    "webId=39472cb9d61e648e6a4e5d421a017f21; gid=y08DJdiqYJCd; "
    "xsecappid=xhs-pc-web; loadts=1789708700009; "
    "web_session=040069b8214615fe1cddae8199384bb0e9a4bd; "
    "id_token=VjEAAJZqK4g; unread={%22ub%22:1}; websectiga=deadbeef; "
    "sec_poison_id=207f6f51"
)


# ==========================================================================
def cookie_checks() -> None:
    print("\n--- Cookie 裁剪（核心不变量）---")

    slim = xhs.slim_cookie(FULL)
    check_in("裁剪后保留 web_session", "web_session=", slim)
    check("**裁剪后不含 webId**", "webId" not in slim, True)
    check("裁剪后不含 id_token", "id_token" not in slim, True)
    check("裁剪后不含 websectiga", "websectiga" not in slim, True)
    check("裁剪后不含 sec_poison_id", "sec_poison_id" not in slim, True)
    check("裁剪后不含 acw_tc", "acw_tc" not in slim, True)
    # 保留白名单里实测无害的
    check_in("保留 a1", "a1=", slim)
    check_in("保留 gid", "gid=", slim)

    # 没有 web_session -> 视为不可用
    check("缺 web_session 时返回空串",
          xhs.slim_cookie("a1=x; webId=y"), "")

    # 兜底版本：保留其余字段，只剔元凶
    full2 = xhs.full_cookie_without_drop(FULL)
    check("兜底版不含 webId", "webId" not in full2, True)
    check_in("兜底版保留 websectiga（只剔元凶）", "websectiga=", full2)
    check_in("兜底版保留 web_session", "web_session=", full2)
    check("兜底版缺 web_session 时返回空串",
          xhs.full_cookie_without_drop("a1=x"), "")

    # cookie_map 的基本功
    m = xhs.cookie_map("a=1; b=2=3; ; c = 4 ")
    check("cookie_map 只按第一个 = 切", m.get("b"), "2=3")
    check("cookie_map 去空白", m.get("c"), "4")


# ==========================================================================
def url_param_checks() -> None:
    print("\n--- 链接参数解析 ---")

    # 短链跳转后的真实形态（实测抓到的）
    short_final = (
        "https://www.xiaohongshu.com/discovery/item/6aacc077000000002b0245f9"
        "?app_platform=android&ignoreEngage=true&xsec_source=app_share"
        "&xsec_token=CBGgaiqk1vXWoUyacOvKOyOH5H7Tn-PVRHtUpFqdgePa0=&xsec_source=app_share"
    )
    nid, tok, src = xhs._params_from_url(short_final)
    check("短链跳转后取到 id", nid, "6aacc077000000002b0245f9")
    check("取到 xsec_token", tok, "CBGgaiqk1vXWoUyacOvKOyOH5H7Tn-PVRHtUpFqdgePa0=")
    check("取到 xsec_source", src, "app_share")

    # PC 链接
    pc = ("https://www.xiaohongshu.com/explore/6aacc077000000002b0245f9"
          "?xsec_token=ABC123%3D&xsec_source=pc_feed")
    nid, tok, src = xhs._params_from_url(pc)
    check("PC 链接取到 id", nid, "6aacc077000000002b0245f9")
    check("PC 链接 token 已解码", tok, "ABC123=")
    check("PC 链接 source", src, "pc_feed")

    # 没 source 时给默认值
    nid, tok, src = xhs._params_from_url(
        "https://www.xiaohongshu.com/explore/6aacc077000000002b0245f9?xsec_token=T"
    )
    check("缺 xsec_source 时回落 pc_feed", src, "pc_feed")

    # captcha 页：参数藏在 redirectPath 里（原版专门处理过）
    cap = (
        "https://www.xiaohongshu.com/captcha"
        "?redirectPath=%2Fexplore%2F6aacc077000000002b0245f9%3Fxsec_token%3DTT%26xsec_source%3Dpc_feed"
    )
    nid, tok, src = xhs._params_from_url(cap)
    check("captcha redirectPath 里取到 id", nid, "6aacc077000000002b0245f9")
    check("captcha redirectPath 里取到 token", tok, "TT")
    check("captcha redirectPath 里取到 source", src, "pc_feed")

    # 没有 id 的链接
    nid, tok, src = xhs._params_from_url("https://www.xiaohongshu.com/explore")
    check("无 id 时返回 None", nid, None)


# ==========================================================================
def link_regex_checks() -> None:
    print("\n--- 识别正则 ---")
    good = [
        "https://xhslink.com/o/abc123",
        "https://xhslink.cn/o/JhGswOSkZi",
        "https://www.xiaohongshu.com/explore/6aacc077000000002b0245f9?xsec_token=X",
        "https://xiaohongshu.com/discovery/item/6aacc077000000002b0245f9",
    ]
    for u in good:
        check_in(f"命中 {u[:52]}", u.split("?")[0], xhs._LINK_RE.search(u).group(0))
    bad = ["https://www.douyin.com/x", "https://xhslink.com", "随便一段文字"]
    for u in bad:
        check(f"不命中 {u[:40]}", bool(xhs._LINK_RE.search(u)), False)


# ==========================================================================
def state_parse_checks() -> None:
    print("\n--- __INITIAL_STATE__ 解析 ---")

    # undefined 必须被替换，否则 JSON 解析失败
    html = (
        "<script>window.__INITIAL_STATE__="
        '{"note":{"noteDetailMap":{"abc":{"note":{"title":"t","type":"normal",'
        '"imageList":[{"urlDefault":"http://i/1.webp"}]}}}},'
        '"foo":undefined,"bar":{"baz":undefined}}</script></html>'
    )
    state = xhs._parse_state(html)
    check("含 undefined 的 state 能解析", state is not None)
    if state:
        check("undefined 被替换为 null", state.get("foo"), None)
    check("没有 __INITIAL_STATE__ 时返回 None", xhs._parse_state("<html></html>"), None)
    check("坏 JSON 时返回 None",
          xhs._parse_state('<script>window.__INITIAL_STATE__=not-json</script>'), None)

    # NaN 兜底
    html2 = ('<script>window.__INITIAL_STATE__={"a":NaN,"b":1}</script>')
    check("含 NaN 也能解析（第二层兜底）", xhs._parse_state(html2) is not None)


def note_extract_checks() -> None:
    print("\n--- 笔记内容提取 ---")

    note = {
        "type": "normal",
        "title": "",
        "desc": "作者涣渌小集#罗小黑[话题]# #分享[话题]#\n手机亮度调到最高有不一样的感觉",
        "user": {"nickname": "禄赴小录"},
        "imageList": [
            {"urlDefault": "http://i/1.webp"},
            {"url": "http://i/2.webp"},          # 没有 urlDefault，回落到 url
            {"urlDefault": ""},                   # 空值应被跳过
        ],
    }
    imgs, vids = xhs._extract_media(note)
    check("图片提取 2 张（跳过空值）", len(imgs), 2)
    check("优先用 urlDefault", imgs[0], "http://i/1.webp")
    check("回落到 url 字段", imgs[1], "http://i/2.webp")
    check("图文没有视频", vids, [])

    # 视频笔记（旧结构：stream 键是 h264）
    vnote = {
        "type": "video",
        "imageList": [{"urlDefault": "http://i/cover.webp"}],
        "video": {
            "media": {"stream": {"h264": [
                {"masterUrl": "http://v/720p.mp4"},
                {"masterUrl": "http://v/1080p.mp4"},
            ]}},
            "consumer": {"originVideoKey": "abc/origin.mp4"},
        },
    }
    imgs, vids = xhs._extract_media(vnote)
    check("旧 h264 结构也能取到候选", len(vids), 2)
    check("无分辨率信息时保持原顺序", vids[0], "http://v/720p.mp4")

    # 真实结构：stream 的键是编码器代号 EF4/EF5/…，不是 h264。
    # 且 streamType 数字与分辨率**没有单调关系**（76 反而最大），
    # 所以必须按「总像素」排序 —— 这条断言防止后人改回按 streamType 排。
    vnote_real = {
        "type": "video",
        "imageList": [{"urlDefault": "http://i/cover.webp"}],
        "video": {"media": {"stream": {
            "EF4": [{"streamType": 259, "width": 720, "height": 900,
                     "videoBitrate": 182426, "masterUrl": "http://v/259.mp4"}],
            "EF6": [],
            "EF5": [
                {"streamType": 301, "width": 1080, "height": 1350,
                 "masterUrl": "http://v/301.mp4"},
                {"streamType": 309, "width": 720, "height": 900,
                 "masterUrl": "http://v/309.mp4"},
                {"streamType": 76, "width": 1800, "height": 1440,
                 "videoBitrate": 249973, "masterUrl": "http://v/76.mp4"},
            ],
            "EF7": [],
        }}},
    }
    _, vids_real = xhs._extract_media(vnote_real)
    check("从 EF* 键取到全部候选（跳过空列表）", len(vids_real), 4)
    check("总像素最大的排第一（1800x1440，streamType 才 76）",
          vids_real[0], "http://v/76.mp4")
    check("其次 1350x1080", vids_real[1], "http://v/301.mp4")
    check("streamType=301 不比 259 优", vids_real[2] in
          ("http://v/259.mp4", "http://v/309.mp4"), True)

    # 没有 masterUrl 时用 originVideoKey 拼
    vnote2 = {
        "video": {"media": {"stream": {}},
                  "consumer": {"originVideoKey": "abc/origin.mp4"}},
        "imageList": [],
    }
    _, vids2 = xhs._extract_media(vnote2)
    check("无 masterUrl 时用 originVideoKey 兜底", len(vids2), 1)
    check_in("兜底 URL 前缀正确", "sns-video-bd.xhscdn.com/abc/origin.mp4", vids2[0])

    # 话题标签规整
    check("话题标签去掉 [话题] 后缀",
          xhs._strip_topic_marks("#罗小黑[话题]# #分享[话题]#"),
          "#罗小黑# #分享#")
    check("普通井号不动", xhs._strip_topic_marks("价格#1"), "价格#1")

    # _pick_note
    state = {"note": {"noteDetailMap": {
        "nid": {"note": {"title": "T"}},
    }}}
    check("按 id 取到 note", xhs._pick_note(state, "nid"), {"title": "T"})
    check("id 对不上时取第一个有 note 的",
          xhs._pick_note(state, "other"), {"title": "T"})
    check("空 map 返回 None", xhs._pick_note({"note": {"noteDetailMap": {}}}, "x"), None)


def result_shape_checks() -> None:
    """锁死「画质候选」不能被当成「多条视频」发出去。

    main.py 的 `_build_video_chain` 会把 `result.videos` 里的每一条都发出去
    （那是给抖音动图设计的），所以小红书只能把最好的那条放进 `videos`，
    其余塞进 `extra["video_backups"]` 供下载失败时重试。
    """
    print("\n--- 结果形状 ---")
    src = (_ROOT / "platforms" / "xiaohongshu.py").read_text(encoding="utf-8")
    check_in("videos 只取画质最好的一条", "videos = video_candidates[:1]", src)
    check_in("其余候选放 video_backups", '"video_backups": backups', src)

    # main.py 的下载重试要用到这个字段
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    check_in("下载失败时会试 video_backups",
             'result.extra.get("video_backups")', main_src)


def main() -> int:
    cookie_checks()
    url_param_checks()
    link_regex_checks()
    state_parse_checks()
    note_extract_checks()
    result_shape_checks()

    print()
    print("=" * 62)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("有失败项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
