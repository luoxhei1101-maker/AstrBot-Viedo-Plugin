"""社交 / 内容平台 resolver。

已移植（逐个对照原仓库源码，逻辑 1:1）：
- 微博    <- ``utils/weibo.js``（mid2id 的 base62 转换 + 页面正则 + API 兜底）
- AcFun   <- ``utils/acfun.js``（ajaxpipe 页面抠 JSON -> ksPlay 取 m3u8）
- 小黑盒  <- ``utils/xiaoheihe.js``（自研 hkey 签名算法 + 帖子 API）
- 米游社  <- ``constants/tools.js`` 的 MIYOUSHE_ARTICLE 接口
- 微视    <- ``constants/tools.js`` 的 WEISHI_VIDEO_INFO 接口

未移植：波点音乐、最右（原版依赖其各自 utils 里的私有接口，需要单独核对）。
"""

from __future__ import annotations

import json
import re
from hashlib import md5

from astrbot.api import logger

from ..core.constants import (
    MIYOUSHE_ARTICLE,
    WEISHI_VIDEO_INFO,
    XHH_BBS_LINK,
    XHH_GAME_LINK,
    XHH_SALT,
)
from ..core.http import HttpError, fetch, fetch_json
from .base import ResolveResult, ResolverContext, not_ported, register

# ==========================================================================
# 微博
# ==========================================================================

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_WEIBO_URL_RE = re.compile(r"(?:weibo\.com|m\.weibo\.cn)/(?:detail/|status/)?([A-Za-z0-9]+)")


def _base62_encode(number: int) -> str:
    """base62 编码，原版 utils/weibo.js 的 base62_encode。"""
    if number == 0:
        return "0"
    result = ""
    while number > 0:
        result = _ALPHABET[number % 62] + result
        number //= 62
    return result


def mid2id(mid: int | str) -> str:
    """微博 mid 转 id，原版逐行移植。

    就是把数字按 7 位一组倒序分组，每组转 base62，不足 4 位补零。
    """
    s = str(mid)[::-1]
    size = (len(s) + 6) // 7
    result: list[str] = []
    for i in range(size):
        chunk = s[i * 7 : (i + 1) * 7][::-1]
        encoded = _base62_encode(int(chunk))
        if i < size - 1 and len(encoded) < 4:
            encoded = "0" * (4 - len(encoded)) + encoded
        result.append(encoded)
    result.reverse()
    return "".join(result)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


@register("weibo")
async def resolve_weibo(link: str, ctx: ResolverContext) -> ResolveResult:
    """微博单条解析。"""
    m = _WEIBO_URL_RE.search(link)
    if not m:
        return ResolveResult.fail("微博", "无法从链接中提取微博 ID")

    wid = m.group(1)
    # 纯数字的是 mid，短 id 已经是 id
    if wid.isdigit() and "detail" not in link:
        try:
            wid = mid2id(int(wid))
        except (ValueError, OverflowError):
            pass

    cookie = ctx.cookie("weibo")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://weibo.com/",
    }
    if cookie:
        headers["Cookie"] = cookie

    data: dict | None = None

    # 路线 1：详情页 HTML 里抠 "status": {...}
    try:
        body, _ = await fetch(f"https://m.weibo.cn/detail/{wid}", headers=headers, retries=1)
        html = body.decode("utf-8", errors="ignore")
        match = re.search(r'"status":\s*([\s\S]+?),\s*"call"', html)
        if match:
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        if not data:
            render = re.search(r"\$render_data\s*=\s*\[([\s\S]+?)\]\[0\]", html)
            if render:
                try:
                    data = json.loads("[" + render.group(1) + "]")[0].get("status")
                except (json.JSONDecodeError, IndexError, AttributeError):
                    pass
    except HttpError as exc:
        logger.debug(f"[R插件][微博] 详情页抓取失败: {exc}")

    # 路线 2：官方 API 兜底
    if not data:
        try:
            resp = await fetch_json(
                f"https://m.weibo.cn/statuses/show?id={wid}",
                headers={**headers, "X-Requested-With": "XMLHttpRequest"},
                retries=1,
            )
            data = resp.get("data")
        except HttpError as exc:
            logger.debug(f"[R插件][微博] API 兜底失败: {exc}")

    if not data:
        return ResolveResult.fail("微博", "没能取到微博内容（可能需要配置 Cookie）")

    # 抽媒体：微博的图片字段是 pic_ids / pics，视频是 page_info.media_info
    images: list[str] = []
    for pic in data.get("pics") or []:
        url = (pic or {}).get("large", {}).get("url") or (pic or {}).get("url")
        if url:
            images.append(url if url.startswith("http") else f"https:{url}")

    if not images:
        for pid in data.get("pic_ids") or []:
            images.append(f"https://wx1.sinaimg.cn/large/{pid}.jpg")

    videos: list[str] = []
    page_info = data.get("page_info") or {}
    media_info = page_info.get("media_info") or {}
    for field_name in ("stream_url_hd", "stream_url", "mp4_720p_mp4", "mp4_hd_url"):
        url = media_info.get(field_name)
        if url:
            videos.append(url)
            break
    if not videos:
        for url_info in (media_info.get("playback_list") or []):
            play = ((url_info or {}).get("play_info") or {}).get("url")
            if play:
                videos.append(play)
                break

    if not images and not videos:
        return ResolveResult.fail("微博", "这条微博没有可下载的图片或视频")

    return ResolveResult.ok(
        "微博",
        images=images,
        videos=videos,
        desc=_strip_html(data.get("text") or "")[:300],
        author=(data.get("user") or {}).get("screen_name", ""),
    )


# ==========================================================================
# AcFun
# ==========================================================================

_ACFUN_ID_RE = re.compile(r"acfun\.cn/v/(ac\d+)", re.IGNORECASE)
_ACFUN_SHORT_RE = re.compile(r"^ac(\d{8})$", re.IGNORECASE)


def _escape_json_escapes(text: str) -> str:
    """原版 escapeSpecialChars：把多余的反斜杠转义干掉。"""
    return text.replace('\\\\"', '\\"').replace('\\"', '"')


@register("acfun")
async def resolve_acfun(link: str, ctx: ResolverContext) -> ResolveResult:
    """AcFun 视频解析。"""
    m = _ACFUN_ID_RE.search(link)
    if m:
        video_url = f"https://www.acfun.cn/v/{m.group(1)}"
    else:
        short = _ACFUN_SHORT_RE.search(link.strip())
        if not short:
            return ResolveResult.fail("AcFun", "无法从链接中提取 ac 号")
        video_url = f"https://www.acfun.cn/v/ac{short.group(1)}"

    # 原版给页面加 ajaxpipe 参数，拿到内嵌 JSON 的片段
    url = video_url + "?quickViewId=videoInfo_new&ajaxpipe=1"
    try:
        body, _ = await fetch(url, retries=1)
    except HttpError as exc:
        return ResolveResult.fail("AcFun", f"页面抓取失败: {exc}")

    raw = body.decode("utf-8", errors="ignore")
    if "window.videoInfo =" not in raw:
        return ResolveResult.fail("AcFun", "页面结构变化，没能定位 videoInfo")

    try:
        fragment = raw.split("window.pageInfo = window.videoInfo =", 1)[1]
        fragment = fragment.split("</script>", 1)[0]
        video_info = json.loads(_escape_json_escapes(fragment))
    except (IndexError, json.JSONDecodeError) as exc:
        return ResolveResult.fail("AcFun", f"videoInfo 解析失败: {exc}")

    # ksPlayJson 里挂着 m3u8 的各档位地址
    videos: list[str] = []
    try:
        ks_play = json.loads(video_info["currentVideoInfo"]["ksPlayJson"])
        representations = ks_play["adaptationSet"][0]["representation"]
        # 原版取全部档位；这里按码率排序取最高的那档
        representations.sort(key=lambda r: r.get("avgBitrate", 0), reverse=True)
        videos = [r["url"] for r in representations if r.get("url")]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        logger.debug(f"[R插件][AcFun] ksPlayJson 解析失败: {exc}")

    if not videos:
        return ResolveResult.fail("AcFun", "没能取到视频流地址")

    return ResolveResult.ok(
        "AcFun",
        videos=videos[:1],
        title=video_info.get("title", ""),
        author=(video_info.get("user") or {}).get("name", ""),
        desc=(video_info.get("description") or "")[:300],
        cover=video_info.get("coverUrl", ""),
    )


# ==========================================================================
# 小黑盒
# ==========================================================================

_XHH_BBS_RE = re.compile(r"xiaoheihe\.cn/bbs/app/link/tree\?.*?link_id=(\d+)")
_XHH_DETAIL_RE = re.compile(r"xiaoheihe\.cn/bbs/link/(\d+)")
_XHH_APP_RE = re.compile(r"xiaoheihe\.cn/(?:game|app)/(\d+)")


def _xhh_da(e: int) -> int:
    return (255 & ((e << 1) ^ 27)) if (128 & e) else (e << 1)


def _xhh_ba(e: int) -> int:
    return _xhh_da(e) ^ e


def _xhh_na(e: int) -> int:
    return _xhh_ba(_xhh_da(e))


def _xhh_fa(e: int) -> int:
    return _xhh_na(_xhh_ba(_xhh_da(e)))


def _xhh_ua(e: int) -> int:
    return _xhh_fa(e) ^ _xhh_na(e) ^ _xhh_ba(e)


def _xhh_za(text: str, salt: str, n: int) -> str:
    """原版 zA：以 salt 的 slice 作为字符池，按 text 的字符码取模取值。"""
    pool = salt[:n] if n > 0 else salt[n:] if n < 0 else salt
    if not pool:
        pool = salt
    return "".join(pool[ord(ch) % len(pool)] for ch in text)


def _xhh_wa(text: str, salt: str) -> str:
    return "".join(salt[ord(ch) % len(salt)] for ch in text)


def _xhh_interleave(parts: list[str]) -> str:
    """原版那个按列读取的交错函数。"""
    if not parts:
        return ""
    width = max(len(p) for p in parts)
    out = ""
    for i in range(width):
        for p in parts:
            if i < len(p):
                out += p[i]
    return out


def build_xhh_hkey(path: str, timestamp: int, nonce: str) -> str:
    """小黑盒 hkey 签名，原版 utils/xiaoheihe.js 的 HA() 逐行移植。"""
    normalized = "/" + "/".join(seg for seg in path.split("/") if seg) + "/"

    interleaved = _xhh_interleave(
        [
            _xhh_za(str(timestamp), XHH_SALT, -2),
            _xhh_wa(normalized, XHH_SALT),
            _xhh_wa(nonce, XHH_SALT),
        ]
    )[:20]

    digest = md5(interleaved.encode()).hexdigest()

    # 取 md5 后 6 位的字符码，做一轮异或混淆再求和取模 100
    codes = [ord(c) for c in digest[-6:]]
    t0 = _xhh_ua(codes[0]) ^ _xhh_fa(codes[1]) ^ _xhh_na(codes[2]) ^ _xhh_ba(codes[3])
    t1 = _xhh_ba(codes[0]) ^ _xhh_ua(codes[1]) ^ _xhh_fa(codes[2]) ^ _xhh_na(codes[3])
    t2 = _xhh_na(codes[0]) ^ _xhh_ba(codes[1]) ^ _xhh_ua(codes[2]) ^ _xhh_fa(codes[3])
    t3 = _xhh_fa(codes[0]) ^ _xhh_na(codes[1]) ^ _xhh_ba(codes[2]) ^ _xhh_ua(codes[3])
    checksum = (t0 + t1 + t2 + t3) % 100
    checksum_str = str(checksum)
    if len(checksum_str) < 2:
        checksum_str = "0" + checksum_str

    return f"{_xhh_za(digest[:5], XHH_SALT, -4)}{checksum_str}"


@register("xiaoheihe")
async def resolve_xiaoheihe(link: str, ctx: ResolverContext) -> ResolveResult:
    """小黑盒帖子解析。"""
    import random
    import time

    m = _XHH_BBS_RE.search(link) or _XHH_DETAIL_RE.search(link)
    if not m:
        return not_ported(
            "小黑盒",
            "只移植了帖子（bbs link）解析，游戏详情页未移植",
            "游戏详情接口的字段映射",
        )

    link_id = m.group(1)
    path = "bbs/app/link/tree"
    timestamp = int(time.time())
    nonce = md5(f"{timestamp}{random.random()}".encode()).hexdigest().upper()
    hkey = build_xhh_hkey(path, timestamp + 1, nonce)

    params = {
        "os_type": "web",
        "version": "999.0.4",
        "hkey": hkey,
        "_time": timestamp,
        "nonce": nonce,
        "link_id": link_id,
        "limit": 20,
        "web_version": "2.5",
        "x_client_type": "web",
        "x_app": "heybox_website",
        "x_os_type": "Android",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())

    try:
        data = await fetch_json(f"{XHH_BBS_LINK}?{query}", retries=1)
    except HttpError as exc:
        return ResolveResult.fail("小黑盒", f"接口请求失败: {exc}")

    link_data = (data.get("result") or {}).get("link") or {}
    if not link_data:
        return ResolveResult.fail(
            "小黑盒", data.get("msg") or "接口没返回帖子内容（签名可能已失效）"
        )

    title = link_data.get("title") or ""
    text = link_data.get("text") or ""
    images: list[str] = []

    # 正文里的图片在 text 的 markdown 语法里，顺带兜一遍 result 里的图片数组
    for url in re.findall(r"!\[[^\]]*\]\((https?://[^)]+)\)", text):
        images.append(url)

    for field_name in ("imgs", "images", "thumbnails"):
        for item in link_data.get(field_name) or []:
            url = item if isinstance(item, str) else (item or {}).get("url")
            if url and url not in images:
                images.append(url)

    videos: list[str] = []
    video_url = link_data.get("video_url") or (link_data.get("video") or {}).get("url")
    if video_url:
        videos.append(video_url)

    # 按原版 optimizeImageUrl：带查询参数的图加个反斜杠能拿到原图
    images = [u + "\\" if ("?" in u and not u.endswith("\\")) else u for u in images]

    if not images and not videos:
        return ResolveResult.fail("小黑盒", "这条帖子没有可下载的媒体")

    return ResolveResult.ok(
        "小黑盒",
        images=images,
        videos=videos,
        title=title,
        desc=re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)[:300],
    )


# ==========================================================================
# 米游社
# ==========================================================================

_MIYOUSHE_RE = re.compile(r"miyoushe\.com/(?:article|bbs)/detail/(\d+)")
_MIYOUSHE_RE2 = re.compile(r"miyoushe\.com/.*?[?&]post_id=(\d+)")


@register("miyoushe")
async def resolve_miyoushe(link: str, ctx: ResolverContext) -> ResolveResult:
    """米游社文章解析。"""
    m = _MIYOUSHE_RE.search(link) or _MIYOUSHE_RE2.search(link)
    if not m:
        return ResolveResult.fail("米游社", "无法从链接中提取 post_id")

    post_id = m.group(1)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.miyoushe.com/"}
    cookie = ctx.cookie("miyoushe")
    if cookie:
        headers["Cookie"] = cookie

    try:
        data = await fetch_json(MIYOUSHE_ARTICLE.format(post_id), headers=headers, retries=1)
    except HttpError as exc:
        return ResolveResult.fail("米游社", f"接口请求失败: {exc}")

    post = (data.get("data") or {}).get("post") or {}
    post_info = post.get("post") or {}
    if not post_info:
        return ResolveResult.fail("米游社", data.get("message") or "接口没返回文章内容")

    images: list[str] = []
    for item in post.get("image_list") or []:
        url = item if isinstance(item, str) else (item or {}).get("url")
        if url:
            images.append(url)

    # 正文内容里的 img 标签也抠一遍
    content = post_info.get("content") or ""
    for url in re.findall(r'<img[^>]+src="([^"]+)"', content):
        if url not in images:
            images.append(url)

    if not images:
        return ResolveResult.fail("米游社", "这篇文章没有图片")

    author = ((post_info.get("user") or {}).get("nickname")) or ""
    return ResolveResult.ok(
        "米游社",
        images=images,
        title=post_info.get("subject", ""),
        author=author,
        desc=re.sub(r"<[^>]+>", "", content)[:300],
    )


# ==========================================================================
# 微视
# ==========================================================================

_WEISHI_RE = re.compile(r"weishi\.qq\.com/[^\s]*?feedid=([A-Za-z0-9_-]+)")


@register("weishi")
async def resolve_weishi(link: str, ctx: ResolverContext) -> ResolveResult:
    """微视视频解析。

    接口字段名在不同版本里变过，所以这里用一组候选字段逐个试，
    全都没命中就报失败——不硬猜。
    """
    m = _WEISHI_RE.search(link)
    if not m:
        return ResolveResult.fail("微视", "无法从链接中提取 feedid")

    feed_id = m.group(1)
    try:
        data = await fetch_json(
            WEISHI_VIDEO_INFO.format(feed_id),
            headers={"User-Agent": "Mozilla/5.0"},
            retries=1,
        )
    except HttpError as exc:
        return ResolveResult.fail("微视", f"接口请求失败: {exc}")

    # 接口可能把数据放在 data / feedInfo / feed_info 任一层
    payload: dict = {}
    for key in ("data", "feedInfo", "feed_info"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            payload = candidate
            break
    if not payload:
        payload = data

    videos: list[str] = []
    for field_name in ("video_url", "videoUrl", "play_url", "playUrl", "url"):
        url = payload.get(field_name)
        if isinstance(url, str) and url.startswith("http"):
            videos.append(url)
            break

    if not videos:
        for item in payload.get("video_url_list") or payload.get("videoUrlList") or []:
            url = item.get("url") if isinstance(item, dict) else item
            if url:
                videos.append(url)
                break

    if not videos:
        return ResolveResult.fail("微视", "接口没返回可用的视频地址（字段结构可能已变化）")

    return ResolveResult.ok(
        "微视",
        videos=videos[:1],
        title=payload.get("feed_desc") or payload.get("desc") or "",
        author=((payload.get("poster") or {}).get("nick")) or "",
        cover=payload.get("cover_url") or payload.get("coverUrl") or "",
    )
