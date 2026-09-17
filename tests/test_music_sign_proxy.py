"""点歌签名代理与列表图的离线断言（不联网、不依赖 AstrBot 运行时）。

覆盖四件容易回归的事：

1. 上游「双重编码」响应必须被展开一层（否则协议端校验失败、卡片不显示）
2. 请求里的 ``type`` 必须按歌曲页域名改成平台对应值（这是卡片品牌
   「网易云音乐 / QQ音乐」的**唯一**来源，必须在签名前设定）
3. ``content`` 要映射成 ``singer``（上游只认后者）
4. 列表图能画出来且尺寸合理（缺 Pillow/字体时返回 None 而不是抛异常）

⚠️ 测试里**故意不包含**任何「改写已签名卡片内容」的断言 —— token 是内容摘要，
改了就让签名失效。相关教训见 core/music_sign_proxy.py 的模块 docstring。
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
    PLATFORM_TYPES,
    SignProxy,
    normalize_sign_request,
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
print("2) normalize_sign_request —— 按域名指定平台 type（卡片品牌的来源）")
print("=" * 66)


def norm(payload: dict) -> dict:
    return json.loads(normalize_sign_request(json.dumps(payload).encode()))


base = {
    "type": "custom",
    "url": "https://music.163.com/#/song?id=186016",
    "audio": "https://m801.music.126.net/a/1.mp3",
    "title": "晴天",
    "image": "https://p1.music.126.net/a/1.jpg",
    "content": "周杰伦",
}

r = norm(base)
check(r["type"] == "163", "网易云链接 -> type 改为 163（上游据此签出「网易云音乐」）")
check(r["singer"] == "周杰伦", "content 映射成 singer")
check(r["url"] == base["url"] and r["audio"] == base["audio"], "其余字段原样保留")

r = norm({**base, "url": "https://y.qq.com/n/ryqq/songDetail/003Qui1q2u1Zho"})
check(r["type"] == "custom", "QQ音乐链接 -> 保持 custom（上游给「QQ音乐」品牌）")

r = norm({**base, "url": "https://www.kugou.com/song/#hash=x"})
check(r["type"] == "kugou", "酷狗链接 -> type=kugou")

r = norm({**base, "url": "https://unknown.example/song/1"})
check(r["type"] == "custom", "未知域名 -> type 不变")

# 已有 singer 时不覆盖
r = norm({**base, "singer": "原唱"})
check(r["singer"] == "原唱", "已有 singer 时不覆盖")

# id 模式原样转发（上游已停用，交给它自己报错）
r = norm({"type": "163", "id": 186016})
check(r["type"] == "163" and r["id"] == 186016, "id 模式原样转发")

# 健壮性：非 JSON / 非对象 / 空 都不该抛异常
check(normalize_sign_request(b"not json") == b"not json", "非 JSON 原样返回")
check(normalize_sign_request(b"[1,2]") == b"[1,2]", "非对象原样返回")
check(normalize_sign_request(b"") == b"", "空 body 原样返回")

# 幂等：跑两次结果一致
once = normalize_sign_request(json.dumps(base).encode())
twice = normalize_sign_request(once)
check(json.loads(once) == json.loads(twice), "重复归一化结果一致（幂等）")

check(all(isinstance(d, str) and isinstance(t, str) for d, t in PLATFORM_TYPES),
      "PLATFORM_TYPES 结构正确")

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
