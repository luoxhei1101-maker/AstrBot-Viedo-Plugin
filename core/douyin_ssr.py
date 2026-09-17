"""抖音解析内核 —— 原版 ``utils/douyin.js`` + ``apps/tools.js`` 的忠实移植。

抖音有**两条路**，本模块两条都实现：

1. **主接口**：带 Cookie 请求 ``aweme/v1/web/aweme/detail`` —— 需要
   ``a-bogus`` 签名（463 行混淆 JS，见 ``core/a_bogus.py``）。
2. **SSR 分享页**：请求 ``iesdouyin.com/share/{video,note}/{id}/``，页面里
   内嵌 ``window._ROUTER_DATA`` 直接挖出作品数据，**不需要签名**。

通道选择不是"能用哪个用哪个"，而是**分场景**（见 ``resolve_by_ssr``）：

- ``share/slides`` 链接 → **只能**用主接口。该页是客户端 SPA，HTML 里没有
  ``_ROUTER_DATA``，SSR 一点数据都拿不到。
- 有 Cookie + 有 node → **优先**主接口。
- 否则 → SSR（免登录，但没有动图信息，见下）。

**为什么动图必须走主接口（实测结论，踩过的坑）**：

========================  =============  ==================  ============
通道                      aweme_type     images[].video      能否识别动图
========================  =============  ==================  ============
主接口（a-bogus）        **68**         **有完整视频轨**    ✅
SSR ``share/note``        **2**          **None（被抹掉）**  ❌
SSR ``share/slides``      无 ``_ROUTER_DATA``，拿不到数据   ❌
========================  =============  ==================  ============

SSR 页面**会把动图降级成静态图**：``aweme_type`` 从 68 改写成 2，
``images[].video`` 整个抹掉只留 ``url_list``。所以在 SSR 路径下怎么改判断
逻辑都识别不出动图——这就是"动图发出来还是静图"的根因。原版对
``share/slides`` 也是专走主接口（``apps/tools.js:548``）。

几个容易踩的点，都在代码里标了：

- ``aweme_type`` 是判断视频/图文的**唯一可靠依据**。图集的
  ``video.play_addr`` 里装的是**背景音乐**，按视频发出去就是一条指向 mp3 的
  "视频"。
- 图集**可混排**静态图与动图。动图标志是**该项自带视频轨**，必须**逐项**
  判断（``album_items``），不能按"整条作品有没有动图"一刀切——那样混合
  图集里的静态图会被丢掉。
- 画质探测用 ``Range: bytes=0-1``：完整下载太重，而且同一个 video_id 在不同
  ratio 下**可能返回同一个文件**（档位不存在时服务端会退回默认档），所以要比
  对 Content-Length 去重，否则会把同一个档位当成四个。
- ``_ROUTER_DATA`` 有两种形态：直接是 JSON 对象，或者 ``JSON.parse("...")``
  包一层字符串。只处理前者会漏掉一部分页面。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from astrbot.api import logger

from .a_bogus import generate_a_bogus, node_available
from .constants import (
    DY_COMPRESSED_PLAY_RATIOS,
    DY_INFO,
    DY_PLAY_RATIOS,
    DY_SHARE_NOTE_PAGE,
    DY_SHARE_SLIDES_PAGE,
    DY_SHARE_VIDEO_PAGE,
    DY_TOUTIAO_INFO,
    DY_TTWID_PAYLOAD,
    DY_TTWID_REGISTER,
    DY_TYPE_MAP,
)
from .http import HttpError, fetch, fetch_head_info, post_json

# 原版 constants/constant.js 里的 COMMON_USER_AGENT，照抄
COMMON_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.25 "
    "Mobile Safari/537.36"
)
DOUYIN_REFERER = "https://www.douyin.com/"

_ID_PATTERNS = (
    re.compile(r"share/slides/(\d+)"),
    re.compile(r"share/video/(\d+)"),
    re.compile(r"share/note/(\d+)"),
    re.compile(r"video/(\d+)"),
    re.compile(r"note/(\d+)"),
    re.compile(r"live\.douyin\.com/(\d+)"),
    re.compile(r"/live/(\d+)"),
    re.compile(r"webcast\.amemv\.com/douyin/webcast/reflow/(\d+)"),
    re.compile(r"modal_id=(\d+)"),
)


def douyin_headers(ttwid: str = "") -> dict[str, str]:
    """原版 ``getDouyinHeaders``。"""
    headers = {"User-Agent": COMMON_USER_AGENT, "Referer": DOUYIN_REFERER}
    if ttwid:
        headers["Cookie"] = f"ttwid={ttwid}"
    return headers


def extract_cookie_value(set_cookie: str, name: str) -> str:
    """从 ``Set-Cookie`` 里抠出某个 cookie 的值。"""
    match = re.search(rf"{re.escape(name)}=([^;]+)", set_cookie or "")
    return match.group(1) if match else ""


def extract_aweme_id(url: str) -> str:
    """从各种形态的抖音 URL 里抽作品 ID。"""
    for pattern in _ID_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return ""


def build_canonical_candidates(url: str) -> list[str]:
    """按原版逻辑排出候选分享页 URL。

    ``note`` 链接优先试 note 页，其余优先试 video 页，另一个作为兜底——
    因为抖音偶尔会把图文作品塞进 video 页，反之亦然。

    ``slides``（新版图集/动图分享页）**不在这里处理**：那个页面是客户端
    SPA，HTML 里根本没有 ``_ROUTER_DATA``，SSR 拿不到任何东西。它必须走
    主接口，见 ``is_slides_url`` / ``fetch_aweme_by_api``。
    """
    aweme_id = extract_aweme_id(url)
    if not aweme_id:
        return [url]

    note_page = DY_SHARE_NOTE_PAGE.format(aweme_id)
    video_page = DY_SHARE_VIDEO_PAGE.format(aweme_id)

    if "iesdouyin.com/share/video/" in url:
        return [url, note_page]
    if "iesdouyin.com/share/note/" in url:
        return [url, video_page]
    if "/note/" in url:
        return [note_page, video_page]
    return [video_page, note_page]


def is_slides_url(url: str) -> bool:
    """是不是新版 ``share/slides`` 链接（必须走主接口）。"""
    return "share/slides" in (url or "")


def extract_balanced_json(source: str, marker: str = "window._ROUTER_DATA") -> str:
    """从 HTML 里截出 ``window._ROUTER_DATA`` 后面那段完整 JSON。

    不能按行切——JSON 里可能嵌着任意内容（含 ``</script>``），必须做大括号
    配对。字符串状态和转义都要跟踪，否则遇到 ``"}"`` 就提前收尾。
    """
    marker_index = source.find(marker)
    if marker_index == -1:
        return ""

    assignment_index = source.find("=", marker_index)
    if assignment_index == -1:
        return ""

    after_assignment = source[assignment_index + 1 :].lstrip()
    if after_assignment.startswith("JSON.parse("):
        # 形态二：window._ROUTER_DATA = JSON.parse("...")，取引号内那一段
        start_quote = source.find('"', assignment_index)
        if start_quote == -1:
            return ""
        escaped = False
        for i in range(start_quote + 1, len(source)):
            char = source[i]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                return source[start_quote : i + 1]
        return ""

    json_start = source.find("{", assignment_index)
    if json_start == -1:
        return ""

    depth = 0
    in_string = False
    string_quote = ""
    escaped = False

    for i in range(json_start, len(source)):
        char = source[i]
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == string_quote:
                in_string = False
                string_quote = ""
            continue

        if char in ('"', "'"):
            in_string = True
            string_quote = char
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[json_start : i + 1]

    return ""


def parse_router_data(html: str) -> dict[str, Any]:
    """解析分享页里的 ``_ROUTER_DATA``。"""
    raw = extract_balanced_json(html)
    if not raw:
        raise ValueError("未找到 window._ROUTER_DATA")

    if raw.startswith('"'):
        # JSON.parse 形态：先解成字符串，再解一层
        return json.loads(json.loads(raw))
    return json.loads(raw)


def find_aweme(node: Any) -> dict[str, Any] | None:
    """递归找出作品对象。

    判定条件是「有 aweme_id，且有视频 uri 或非空 images」——抖音会随版本
    改外层 key（``video_(id)/page`` / ``note_(id)/page``），按结构特征找
    比写死 key 稳。
    """
    if not node:
        return None
    if isinstance(node, list):
        for item in node:
            found = find_aweme(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None

    has_video = bool((node.get("video") or {}).get("play_addr", {}).get("uri"))
    images = node.get("images")
    if node.get("aweme_id") and (
        has_video or (isinstance(images, list) and len(images) > 0)
    ):
        return node

    for value in node.values():
        found = find_aweme(value)
        if found:
            return found
    return None


def first_url(url_object: Any) -> str:
    """取 URL 对象里的地址。原版优先取 ``url_list`` 的**最后一个**。

    有讲究：抖音的 url_list 前面几个常带 ``-sign`` 签名参数、时效短，
    最后一个往往是无签名直链，反而更稳。
    """
    if not isinstance(url_object, dict):
        return ""
    url_list = url_object.get("url_list") or []
    if isinstance(url_list, list) and url_list:
        return url_list[-1] or url_list[0] or ""
    return url_object.get("uri") or ""


def content_type(aweme: dict[str, Any]) -> str:
    """判定作品类型，原版 ``getAwemeContentType``。"""
    mapped = DY_TYPE_MAP.get(aweme.get("aweme_type"))
    if mapped:
        return mapped
    if (aweme.get("video") or {}).get("play_addr", {}).get("uri"):
        return "video"
    if isinstance(aweme.get("images"), list) and aweme["images"]:
        return "image"
    return "unknown"


def cover_url(aweme: dict[str, Any]) -> str:
    """封面：video.cover -> video.origin_cover -> 第一张图。"""
    video = aweme.get("video") or {}
    images = aweme.get("images") or []
    return (
        first_url(video.get("cover"))
        or first_url(video.get("origin_cover"))
        or (first_url(images[0]) if images and isinstance(images[0], dict) else "")
        or (images[0].get("url_list", [None])[0] if images else "")
        or ""
    )


def normalize_duration_seconds(duration: Any) -> int:
    """时长归一化成秒。抖音有时给毫秒、有时给秒，用 1000 做分界。"""
    try:
        value = float(duration or 0)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return int(value / 1000) if value > 1000 else int(value)


def static_image_urls(aweme: dict[str, Any]) -> list[str]:
    """静态图集：取每张**静态图**的 ``url_list[0]``（无水印高清，第一候选）。

    **动图会被跳过**——动图要按视频发，不能混进图片列表。历史上这个函数
    是在 ``has_animated_images``/``animated_image_uris`` 旁边用的：先看整条
    作品有没有动图，再决定图片/视频。那套「整条作品一刀切」的做法对混排
    图集是错的（静态图会被丢掉），现在统一走 ``album_items`` 逐项分流。

    注意：``url_list[0]`` 带 ``-sign`` 签名、时效短，可能 403。真正下载时
    应该配合 ``static_image_candidates`` 逐个尝试，这里只取第一候选。
    """
    return [item["image_url"] for item in album_items(aweme) if item["kind"] == "still"]


def static_image_candidates(aweme: dict[str, Any]) -> list[list[str]]:
    """静态图集：每张静态图返回**全部候选 URL**（去重，保持顺序）。

    抖音 url_list 里有多个 CDN 节点（p3-sign / p11-sign / p5-ex-gddgtc-sign …），
    每个的签名时效不一样——实测同一张图有的 URL 403、有的 200，没有固定哪个
    位置一定可用。所以把全部候选给下载层，逐个尝试，第一个能下的用。
    """
    return [item["image_candidates"] for item in album_items(aweme) if item["image_candidates"]]


def album_items(aweme: dict[str, Any]) -> list[dict[str, Any]]:
    """把图集拆成**有序的逐项媒体列表**，区分静态图与动图。

    这是「动图/静态图混合图集」的核心。抖音一个图集里可以既有普通 JPG，
    也有实况/动图（该项自带视频轨），原版 ``processDouyinImageAlbum``
    就是逐项判断 ``imageItem.video.play_addr_h264`` 存不存在来分流：

    - **有视频轨** → 当视频发（``kind="animated"``）。原始音轨就挂在视频轨里，
      即使不额外合 BGM 也不是静音视频——原版用 ffmpeg 合 BGM 只是为了换成
      作品原声，代价高且要落盘，这里改成发直链（BGM 另外单独发语音）。
    - **没有视频轨** → 当图片发（``kind="still"``）。

    **不能按「整条作品有没有动图」一刀切。** 早期版本是这么干的：只要
    ``has_animated_images`` 为真，就把整条作品标成动图，然后只发视频轨，
    于是混合图集里的普通静态图**全部被丢掉**，用户只收到几张动图。

    Args:
        aweme: 作品对象。

    Returns:
        与 ``aweme["images"]`` 等长的有序列表，每项形如::

            {
                "index": 0,
                "kind": "still" | "animated",
                "image_url": "首个图片直链",          # 动图时为空
                "image_candidates": [...],            # 动图时为空
                "video_uri": "视频轨 uri",            # 静态图时为空
                "video_url": "拼好的播放地址",         # 静态图时为空
            }
    """
    items: list[dict[str, Any]] = []
    for index, image in enumerate(aweme.get("images") or []):
        if not isinstance(image, dict):
            continue

        video = image.get("video") or {}
        video_uri = (video.get("play_addr_h264") or {}).get("uri") or (
            video.get("play_addr") or {}
        ).get("uri") or ""

        if video_uri:
            items.append(
                {
                    "index": index,
                    "kind": "animated",
                    "image_url": "",
                    "image_candidates": [],
                    # 直接给 1080p 模板；要压到 720p 由调用方改写 ratio
                    "video_uri": video_uri,
                    "video_url": DY_TOUTIAO_INFO.replace("{}", video_uri),
                    "duration": normalize_duration_seconds(video.get("duration") or 0),
                }
            )
            continue

        url_list = image.get("url_list") or []
        candidates: list[str] = []
        for url in url_list:
            if url and url not in candidates:
                candidates.append(url)
        if candidates:
            items.append(
                {
                    "index": index,
                    "kind": "still",
                    "image_url": candidates[0],
                    "image_candidates": candidates,
                    "video_uri": "",
                    "video_url": "",
                }
            )

    return items


def _looks_like_audio(url: str) -> bool:
    """判断一个地址是不是音频。"""
    lowered = (url or "").lower()
    return ("ies-music" in lowered) or lowered.endswith((".mp3", ".m4a", ".aac"))


def music_info(aweme: dict[str, Any]) -> dict[str, str]:
    """背景音乐。原版 ``resolveDouyinMusic`` 的取值优先级。

    标题：版权音乐 > 原声标题 > 兜底
    歌手：版权音乐作者 > 原声作者

    **这里补了一条原版没有的兜底。** SSR 分享页对匿名/半登录访问会把
    ``music.play_url`` 整个抹掉（实测是 ``None``），于是原版那套
    ``item.music.play_url.uri`` 判断在 SSR 路径下永远不成立，BGM 永远发不出来。

    但抖音在**图集**里把音频塞进了 ``video.play_addr.uri``：
    实测值是 ``https://lf9-music-east.douyinstatic.com/obj/ies-music-hj/xxx.mp3``
    ——就是 BGM 本体。所以当 ``play_url`` 缺失时，如果 ``play_addr.uri``
    长得像音频（``ies-music`` 域名或音频后缀），就拿它当 BGM。
    """
    music = aweme.get("music") or {}
    play_url = music.get("play_url") or {}

    url = ""
    url_list = play_url.get("url_list") or []
    if url_list:
        url = url_list[0]
    url = url or play_url.get("uri") or ""

    if not url:
        # SSR 兜底：图集的 play_addr 里装的就是音轨
        play_addr_uri = (
            (aweme.get("video") or {}).get("play_addr", {}) or {}
        ).get("uri") or ""
        if _looks_like_audio(play_addr_uri):
            url = play_addr_uri

    pgc = music.get("matched_pgc_sound") or {}
    title = pgc.get("title") or music.get("title") or "抖音BGM"
    author = pgc.get("author") or music.get("author") or ""

    return {"url": url, "title": title, "author": author}


# ==========================================================================
# 画质探测
# ==========================================================================


def _parse_content_size(headers: dict[str, str]) -> int:
    """从响应头里读文件大小。原版 ``parseContentSize``。"""
    content_range = headers.get("content-range", "")
    match = re.search(r"/(\d+)$", str(content_range))
    if match:
        return int(match.group(1))

    size = int(headers.get("content-length") or 0)
    # 原版的门槛：大于 2 字节才算数（小于 2 的多半是占位响应）
    return size if size > 2 else 0


async def probe_quality(video_uri: str, ratio: str) -> dict[str, Any] | None:
    """探测某个画质档位是否真实存在。

    只下 1 个字节，靠 ``Content-Range`` 总长度判断——完整下载太贵。
    """
    play_url = DY_TOUTIAO_INFO.replace("1080p", ratio).replace("{}", video_uri)
    try:
        status, headers, _, final_url = await fetch_head_info(
            play_url,
            headers=douyin_headers(),
            range_bytes="bytes=0-1",
            timeout=15.0,
        )
    except Exception as exc:  # noqa: BLE001
        # 这里必须兜住所有异常，不能只兜 HttpError：`probe_qualities` 用
        # asyncio.gather 并发调用本函数，任何一个协程抛出未捕获异常都会让
        # 整批探测一起失败。单档探测失败只是「这个档位不可用」，返回 None 即可。
        logger.debug(f"[R插件][抖音] 画质探测失败 {ratio}: {exc}")
        return None

    if status >= 400:
        return None

    size = _parse_content_size(headers)
    if not size:
        return None

    return {"ratio": ratio, "size": size, "play_url": play_url, "final_url": final_url}


async def probe_qualities(
    video_uri: str, prefer_compressed: bool = False
) -> list[dict[str, Any]]:
    """按档位从高到低探测，按文件大小去重，返回可用档位列表。

    去重是必须的：某档位不存在时服务端不会报错，而是退回默认档，
    于是四个 ratio 探出来是同一个文件。不去重就会把同一个档位当成四个。

    **并发探测（性能）**：各档位之间完全独立，以前 ``for ... await`` 串行发
    最多 4 个请求，每个 15s 超时——抖音每次解析都白等 2-4 个 RTT。现在用
    ``asyncio.gather`` 一起发，再**按原顺序**遍历去重。

    顺序语义保持不变：仍然是从高画质往低画质走，遇到 ``size`` 重复的
    （说明服务端退回了同一个文件）就跳过，所以选出来的 ``qualities[0]``
    和串行版本完全一致。
    """
    ordered = DY_COMPRESSED_PLAY_RATIOS if prefer_compressed else DY_PLAY_RATIOS

    # 并发探测所有档位；单档失败返回 None，不影响其它档
    probed = await asyncio.gather(
        *(probe_quality(video_uri, ratio) for ratio in ordered),
        return_exceptions=False,
    )

    available: list[dict[str, Any]] = []
    seen_sizes: set[int] = set()

    for result in probed:
        if not result:
            continue
        if result["size"] in seen_sizes:
            continue
        seen_sizes.add(result["size"])
        available.append(result)

    return available


async def register_anonymous_ttwid() -> str:
    """注册一个匿名 ttwid。

    原版注释说得很清楚：这是纯协议接口，任何访客都能拿到，**不是登录态**。
    没有它，SSR 分享页对部分作品会返回空数据。
    """
    body, set_cookie = await post_json(
        DY_TTWID_REGISTER,
        DY_TTWID_PAYLOAD,
        headers=douyin_headers(),
        timeout=15.0,
    )
    ttwid = extract_cookie_value(set_cookie, "ttwid")
    if not ttwid:
        raise HttpError("匿名 ttwid 获取失败（响应里没有 Set-Cookie ttwid）")
    logger.debug("[R插件][抖音] 已注册匿名 ttwid")
    return ttwid


# ==========================================================================
# 主流程
# ==========================================================================


async def fetch_aweme_by_api(aweme_id: str, cookie: str) -> dict[str, Any]:
    """走**主接口** ``aweme/v1/web/aweme/detail``（需 a-bogus + Cookie）拿作品数据。

    这是唯一能拿到**完整图集/动图数据**的通道，理由是实测结论：

    ====================  =============  ====================  ==========
    通道                 aweme_type     images[].video        能否识别动图
    ====================  =============  ====================  ==========
    主接口（a-bogus）    **68**         **有完整视频轨**       ✅
    SSR ``share/note``   **2**          **None（被抹掉）**     ❌
    SSR ``share/slides`` 无 ``_ROUTER_DATA``，完全拿不到数据    ❌
    ====================  =============  ====================  ==========

    也就是说：**SSR 页面会把动图降级成普通静态图**——``aweme_type`` 从 68
    被改写成 2，``images[].video`` 整个抹掉，只留 ``url_list``。所以在 SSR
    路径下无论怎么改判断逻辑都识别不出动图，这就是"动图发出来还是静图"的
    根因。原版对 ``share/slides`` 也是专走主接口（``apps/tools.js:548``）。

    Args:
        aweme_id: 作品 ID。
        cookie: 抖音 Cookie。**必需**——没有它主接口会返回空 ``aweme_detail``。

    Raises:
        HttpError: 没有 Cookie / 没有 node / 请求失败 / 返回里没有作品数据。
    """
    if not cookie:
        raise HttpError("主接口需要抖音 Cookie（图集/动图解析依赖登录态）")
    if not node_available():
        raise HttpError("主接口需要 a-bogus 签名，但本机没有 node")

    api = DY_INFO.replace("{}", aweme_id)
    query = api.split("?", 1)[1]

    signature = await generate_a_bogus(query, COMMON_USER_AGENT)
    url = f"{api}&a_bogus={signature}"

    headers = {
        "Accept-Language": (
            "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2"
        ),
        "User-Agent": COMMON_USER_AGENT,
        # 原版对图集专用逻辑用的是 open.douyin.com 这对 Origin/Referer
        "Origin": "https://open.douyin.com",
        "Referer": "https://open.douyin.com/",
        "Cookie": cookie,
    }

    body, _ = await fetch(url, headers=headers, timeout=25.0)
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError as exc:
        raise HttpError(f"主接口返回的不是 JSON: {exc}") from exc

    aweme = data.get("aweme_detail")
    if not aweme:
        status = data.get("status_code")
        raise HttpError(f"主接口没有返回作品数据（status_code={status}）")

    logger.debug(
        f"[R插件][抖音] 主接口命中 {aweme_id}，"
        f"aweme_type={aweme.get('aweme_type')}，"
        f"images={len(aweme.get('images') or [])}"
    )
    return aweme


async def fetch_aweme(candidates: list[str], cookie: str) -> tuple[dict, str]:
    """依次尝试候选分享页，返回 ``(aweme, 命中地址)``。"""
    last_error: Exception | None = None

    for url in candidates:
        try:
            headers = douyin_headers()
            headers["Referer"] = DOUYIN_REFERER
            headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            )
            headers["Accept-Language"] = "zh-CN,zh;q=0.9"
            if cookie:
                # 用户配了完整 Cookie 就用它；里面有 ttwid，不用再注册匿名
                headers["Cookie"] = cookie

            body, _ = await fetch(url, headers=headers, retries=1, timeout=20.0)
            html = body.decode("utf-8", errors="ignore")
            router_data = parse_router_data(html)
            aweme = find_aweme(router_data)

            if not aweme or content_type(aweme) == "unknown":
                raise ValueError("SSR 页面里没有可识别的抖音内容")

            return aweme, url
        except Exception as exc:  # noqa: BLE001 - 换下一个候选
            last_error = exc
            logger.debug(f"[R插件][抖音] 候选页解析失败 {url}: {exc}")

    raise HttpError(f"未命中可用的抖音 SSR 分享页：{last_error}")


async def resolve_by_ssr(
    url: str,
    *,
    cookie: str = "",
    prefer_compressed: bool = False,
    probe: bool = True,
) -> dict[str, Any]:
    """抖音解析主入口，返回结构化结果。

    **通道选择**（关键）：

    - ``share/slides`` 链接 → **必须**走主接口。该页面是客户端 SPA，HTML 里
      没有 ``_ROUTER_DATA``，SSR 完全无数据可挖。
    - 配了 Cookie 且 node 可用 → **优先主接口**。因为 SSR 页面会把动图的
      ``images[].video`` 抹掉、把 ``aweme_type`` 从 68 降成 2，导致动图被
      误判成静态图。主接口才能拿到完整视频轨。
    - 主接口失败（或没 Cookie / 没 node）→ 回落 SSR，至少还能发普通视频和静态图。

    Args:
        cookie: 用户配置的抖音 Cookie，可为空。
        prefer_compressed: 是否偏好压缩档（对应原版 ``douyinCompression``）。
        probe: 是否做画质探测。关闭则直接用 1080p 模板（省 1-4 次请求）。

    Returns:
        含 ``aweme`` / ``content_type`` / ``video_url`` / ``canonical_url`` 等字段的字典。
    """
    candidates = build_canonical_candidates(url)
    aweme_id = extract_aweme_id(url) or extract_aweme_id(candidates[0])

    if not aweme_id:
        raise ValueError("无法识别抖音 aweme_id")

    slides = is_slides_url(url)
    # 主接口要 a-bogus（node）+ Cookie，两个都满足才可用
    api_ready = bool(cookie) and node_available()
    aweme: dict[str, Any] | None = None
    canonical = url

    if slides or api_ready:
        try:
            aweme = await fetch_aweme_by_api(aweme_id, cookie)
            canonical = DY_SHARE_SLIDES_PAGE.format(aweme_id) if slides else url
        except HttpError as exc:
            if slides:
                # slides 页没有 SSR 兜底可言，直接把原因抛出去让用户看到
                raise HttpError(f"抖音图集/动图解析失败：{exc}") from exc
            logger.debug(f"[R插件][抖音] 主接口不可用，回落 SSR: {exc}")

    if aweme is None:
        aweme, canonical = await fetch_aweme(candidates, cookie)

    resolved: dict[str, Any] = {
        "aweme_id": str(aweme.get("aweme_id") or aweme_id),
        "aweme": aweme,
        "content_type": content_type(aweme),
        "canonical_url": canonical,
        "author": aweme.get("author") or {},
        "author_nickname": (aweme.get("author") or {}).get("nickname") or "抖音用户",
        "desc": aweme.get("desc") or "",
        "duration_seconds": normalize_duration_seconds(
            (aweme.get("video") or {}).get("duration") or aweme.get("duration")
        ),
        "cover_url": cover_url(aweme),
    }

    if resolved["content_type"] != "video":
        return resolved

    video_uri = (aweme.get("video") or {}).get("play_addr", {}).get("uri")
    if not video_uri:
        raise ValueError("SSR 页面中未找到视频播放信息")

    resolved["video_id"] = video_uri
    resolved["video_url"] = DY_TOUTIAO_INFO.replace("1080p", "1080p").replace(
        "{}", video_uri
    )

    if probe:
        try:
            qualities = await probe_qualities(video_uri, prefer_compressed)
        except HttpError as exc:
            logger.debug(f"[R插件][抖音] 画质探测整体失败，退回默认档: {exc}")
            qualities = []

        if qualities:
            best = qualities[0]
            resolved["video_url"] = best["play_url"]
            resolved["selected_ratio"] = best["ratio"]
            resolved["available_ratios"] = [q["ratio"] for q in qualities]

    return resolved
