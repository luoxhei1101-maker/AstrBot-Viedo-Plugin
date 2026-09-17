"""点歌签名代理与列表图的离线断言（不联网、不依赖 AstrBot 运行时）。

覆盖三件容易回归的事：
1. 上游「双重编码」响应必须被展开一层（否则协议端校验失败、卡片不显示）
2. 卡片的平台标识要按 jumpUrl 域名改写（否则点网易云却显示「QQ音乐」）
3. 列表图能画出来且尺寸合理（缺 Pillow/字体时返回 None 而不是抛异常）
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN.parent))

# ---- 桩掉 astrbot.api（只用到 logger）----
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


api.logger = _Logger()
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)

from astrbot_plugin_rconsole.core.music_sign_proxy import (  # noqa: E402
    DEFAULT_UPSTREAM,
    SignProxy,
    fix_platform_tag,
    unwrap_sign_response,
)

PASS = 0
FAIL = 0


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}")


def card(jump: str, tag: str = "QQ音乐") -> str:
    return json.dumps(
        {
            "app": "com.tencent.music.lua",
            "config": {"ctime": 1, "forward": 1, "token": "x" * 32, "type": "normal"},
            "meta": {"music": {"title": "晴天", "desc": "周杰伦",
                               "jumpUrl": jump, "musicUrl": "http://a/b.mp3",
                               "preview": "http://a/c.jpg", "tag": tag}},
            "view": "music",
        },
        ensure_ascii=False,
    )


print("=" * 66)
print("1) unwrap_sign_response —— 展开双重编码")
print("=" * 66)
raw = card("https://y.qq.com/x")
double = json.dumps(raw)          # '"{...}"'  ← 上游返回的就是这种
out = unwrap_sign_response(double)
check(out.strip().startswith("{"), "双重编码被展开成裸 JSON 对象")
check(json.loads(out)["app"] == "com.tencent.music.lua", "展开后内容完整")
check(unwrap_sign_response(raw) == raw, "已是裸 JSON 时原样返回")
check(unwrap_sign_response("not json") == "not json", "非 JSON 时原样返回")
check(unwrap_sign_response("") == "", "空串原样返回")

print()
print("=" * 66)
print("2) fix_platform_tag —— 按 jumpUrl 改写平台标识")
print("=" * 66)
c = json.loads(fix_platform_tag(card("https://music.163.com/#/song?id=1")))
check(c["meta"]["music"]["tag"] == "网易云音乐", "网易云链接 -> tag 改为「网易云音乐」")
check("music.126.net" in c["meta"]["music"]["tagIcon"], "网易云图标已替换")

c = json.loads(fix_platform_tag(card("https://y.qq.com/n/ryqq/songDetail/x")))
check(c["meta"]["music"]["tag"] == "QQ音乐", "QQ音乐链接保持「QQ音乐」")

c = json.loads(fix_platform_tag(card("https://www.kugou.com/song/#hash=x")))
check(c["meta"]["music"]["tag"] == "酷狗音乐", "酷狗链接 -> 「酷狗音乐」")

out = fix_platform_tag(card("https://unknown.example/x"))
c = json.loads(out)
check(c["meta"]["music"]["tag"] == "QQ音乐", "未知域名时保持上游原值不动")

check(fix_platform_tag("not json") == "not json", "非 JSON 原样返回")
check(fix_platform_tag('{"app":"x"}') == '{"app":"x"}', "缺 meta 时原样返回")
check(fix_platform_tag(json.dumps({"meta": {"music": "bad"}}))
      == json.dumps({"meta": {"music": "bad"}}), "meta.music 非对象时原样返回")

print()
print("=" * 66)
print("3) SignProxy —— 上游列表与地址校验")
print("=" * 66)
p = SignProxy("", 18888)
check(DEFAULT_UPSTREAM in p._upstreams(), "默认上游包含官方首选地址")
check(p.url() == "http://astrbot:18888/", "给出容器内可用的 musicSignUrl")
p2 = SignProxy("http://custom.example/sign", 19000)
ups = p2._upstreams()
check(ups[0] == "http://custom.example/sign", "自定义上游排在最前")
check(len(ups) == len(set(ups)), "上游列表去重")
check(p2.url() == "http://astrbot:19000/", "端口可配")
check(not p.running, "未启动时 running 为 False")

print()
print("=" * 66)
print("4) 列表图渲染")
print("=" * 66)
try:
    from astrbot_plugin_rconsole.core.music_card_image import HAS_PIL, render_song_list
except Exception as exc:  # noqa: BLE001
    print(f"  (导入失败: {exc})")
    HAS_PIL = False

if HAS_PIL:
    import asyncio

    class _Song:
        def __init__(self, i):
            self.name = f"歌曲{i}"
            self.artist = f"歌手{i}"
            self.album = "专辑"
            self.cover = ""
            self.page_url = ""

    songs = [_Song(i) for i in range(1, 4)]
    png = asyncio.run(render_song_list(songs, "测试", "网易云音乐", "netease", "回复 1-3"))
    check(isinstance(png, (bytes, bytearray)) and len(png) > 1000,
          f"能画出 PNG（{len(png) if png else 0} 字节）")
    check(png[:8] == b"\x89PNG\r\n\x1a\n", "确实是 PNG 格式")
    empty = asyncio.run(render_song_list([], "x", "y", "netease", ""))
    check(empty is None, "空列表返回 None（调用方退回文字）")
else:
    print("  (跳过：本机没装 Pillow，服务器镜像里有)")

print()
print("=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
