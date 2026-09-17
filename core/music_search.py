"""点歌搜索：网易云 + QQ 音乐。

为什么要单独一个模块
====================

插件原有的音乐能力在 ``platforms/audio.py``，但它只解决「**给一个歌曲分享
链接 → 拿到音频直链**」——前提是用户已经知道那首歌、手里有链接。群里更常见
的需求是反过来：「点歌 晴天」→ 列出候选。原版没有这个能力。

实现参考了 TRSS-Yunzai 的 xiaofei-plugin（``apps/点歌.js``）的多平台抽象
与 QQ 音乐签名流程，**但修正了它几处已经过期的取数路径**（见下）。

登录态要求（2026-09-17 用真实会员账号实测）
==========================================

- **网易云**：搜索 + 取直链**完全匿名可用**。配了 ``MUSIC_U`` 后能解锁
  VIP 歌曲的高音质直链。
- **QQ 音乐**：搜索匿名可用；**取直链必须有登录态 Cookie**。匿名调
  ``CgiGetVkey`` 恒返回 ``result=104003``（= 需要登录/VIP）。
  实测带上会员 Cookie 后，周杰伦《晴天》能拿到 320kbps mp3 直链。

QQ 音乐 Cookie 的字段（**实测确认的对应关系**）：

===========================  ======================================
抓包常见的名字                接口要求的名字
===========================  ======================================
``uid``                      ``uin``
``qqopenid``                 ``psrf_qqopenid``
``qqyunionid``               ``psrf_qqunionid``
``qqaccess_token``           ``psrf_qqaccess_token``（可选）
``psrf_qqrefresh_token``     ``psrf_qqrefresh_token``
``qqmusic_key`` / ``qm_keyst``  （同名，**取直链的真正凭据**）
===========================  ======================================

:func:`parse_cookie` 会自动做这套映射，用户整串粘贴即可。

⚠️ ``qqmusic_key`` 有效期约 **12 小时**，过期后取直链重新失败（仍返回
104003）。届时需重新抓一次，这是 QQ 音乐的机制。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .constants import (
    COMMON_USER_AGENT,
    NETEASE_SEARCH_API,
    NETEASE_SEARCH_COOKIE,
    NETEASE_SONG_OUTER_URL,
    NETEASE_SONG_PAGE,
    NETEASE_SONG_URL_API,
    NETEASE_URL_COOKIE,
    QQ_MUSIC_BODY_TEMPLATE,
    QQ_MUSIC_SEARCH_API,
    QQ_MUSIC_SONG_PAGE,
    QQ_MUSIC_VKEY_API,
)
from .http import HttpError, fetch, fetch_json

# 一次搜索返回的最大条数
MAX_RESULTS = 10

# 平台别名 -> 内部 key
PLATFORM_ALIASES: dict[str, str] = {
    "网易云": "netease",
    "网抑云": "netease",
    "网易": "netease",
    "netease": "netease",
    "qq": "qqmusic",
    "QQ": "qqmusic",
    "qq音乐": "qqmusic",
    "QQ音乐": "qqmusic",
    "qqmusic": "qqmusic",
}

# 默认尝试顺序。网易云匿名就能取到直链，所以排前面。
DEFAULT_ORDER: tuple[str, ...] = ("netease", "qqmusic")

PLATFORM_LABELS: dict[str, str] = {
    "netease": "网易云音乐",
    "qqmusic": "QQ音乐",
}

# 网易云音质档位。注意**上限是 exhigh**——实测传 lossless / hires 都会被
# 服务端降级成 exhigh 返回，所以没必要暴露更高的档位给用户。
NETEASE_LEVELS: dict[str, str] = {
    "standard": "标准",
    "higher": "较高",
    "exhigh": "极高",
}
DEFAULT_LEVEL = "exhigh"


@dataclass
class Song:
    """一首候选歌曲。"""

    platform: str
    """内部平台 key（netease / qqmusic）。"""

    song_id: str
    """平台内唯一标识：网易云用数字 id，QQ 音乐用 songmid。"""

    name: str
    artist: str = ""
    album: str = ""
    cover: str = ""
    page_url: str = ""
    """歌曲网页地址，取直链失败时的兜底。"""

    play_url: str = ""
    """音频直链。搜索阶段为空，按需再取（见 ``get_play_url``）。"""

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """「歌名 - 歌手」的展示串。"""
        return f"{self.name} - {self.artist}" if self.artist else self.name


# ==========================================================================
# 网易云
# ==========================================================================


async def search_netease(keyword: str, limit: int = MAX_RESULTS) -> list[Song]:
    """网易云搜索。

    接口 ``music.163.com/api/cloudsearch/pc`` 是网页版在用的官方接口。
    **匿名可用**——实测 2026-09 匿名返回正常。带 ``MUSIC_U`` 只是让
    搜索结果里 VIP 歌的 privilege 信息更完整，不影响能搜到什么。
    """
    body = f"offset=0&limit={limit}&type=1&s={_urlencode(keyword)}"
    try:
        data = await _post_form(
            NETEASE_SEARCH_API,
            body,
            headers={
                "Referer": "https://music.163.com/",
                "Cookie": NETEASE_SEARCH_COOKIE,
            },
        )
    except HttpError as exc:
        logger.warning(f"[R插件] 网易云搜索请求失败: {exc}")
        return []

    result = data.get("result") or {}
    songs = result.get("songs") or []
    out: list[Song] = []
    for item in songs[:limit]:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "")
        if not sid:
            continue
        artists = "/".join(
            str(a.get("name", ""))
            for a in (item.get("ar") or [])
            if isinstance(a, dict)
        )
        album = item.get("al") or {}
        privilege = item.get("privilege") or {}
        out.append(
            Song(
                platform="netease",
                song_id=sid,
                name=str(item.get("name") or "").strip(),
                artist=artists,
                album=str(album.get("name") or ""),
                cover=str(album.get("picUrl") or ""),
                page_url=NETEASE_SONG_PAGE.format(id=sid),
                extra={
                    # fee: 0 免费 / 1 VIP / 4 需购买 / 8 低音质免费
                    "fee": item.get("fee"),
                    "max_br": privilege.get("maxbr"),
                },
            )
        )
    return out


async def netease_play_url(
    song: Song, cookie: str = "", level: str = DEFAULT_LEVEL
) -> str:
    """取网易云音频直链。

    优先调 ``player/url/v1``（能拿到真实可下载地址）；失败则回落到
    ``song/media/outer/url``——那个 URL 会对可用歌曲 302 到 CDN，
    对不可用歌曲 302 回 ``music.163.com``。

    ``cookie`` 传 ``MUSIC_U=...`` 时能解锁 VIP 歌；传空串则匿名，
    VIP 歌（``fee=1``）会拿到空 url。
    """
    lv = level if level in NETEASE_LEVELS else DEFAULT_LEVEL
    body = (
        f"ids={_json_dumps([int(song.song_id)])}"
        f"&level={lv}&encodeType=mp3"
    )
    cookie_hdr = NETEASE_URL_COOKIE
    if cookie.strip():
        cookie_hdr = f"{cookie.strip()}; {cookie_hdr}"

    try:
        data = await _post_form(
            NETEASE_SONG_URL_API,
            body,
            headers={"Cookie": cookie_hdr, "Referer": "https://music.163.com/"},
        )
        rows = data.get("data") or []
        if rows and isinstance(rows[0], dict):
            url = str(rows[0].get("url") or "")
            if url:
                return url
            # fee=1 是 VIP 独占、fee=4 是需购买，这两种没会员拿不到直链
            fee = rows[0].get("fee")
            logger.info(
                f"[R插件] 网易云直链为空（fee={fee}，"
                f"{'需要会员 Cookie' if fee in (1, 4) else '可能已下架'}）"
            )
    except HttpError as exc:
        logger.warning(f"[R插件] 网易云取直链失败: {exc}")

    return NETEASE_SONG_OUTER_URL.format(id=song.song_id)


# ==========================================================================
# QQ 音乐
# ==========================================================================


async def search_qqmusic(keyword: str, limit: int = MAX_RESULTS) -> list[Song]:
    """QQ 音乐搜索。

    ⚠️ **两个实测坑**（2026-09-17 逐项隔离验证过）：

    1. **不能发 ``Accept-Language`` 头**。带上它接口固定返回
       ``search.code=2001`` + 空 body（2134 字节），不带则正常返回
       ~24–49KB。其余头（UA / Content-Type / Referer / Origin / Accept）
       都无影响，就这一个敏感。所以下面用显式的「干净头集合」，
       不让 ``core/http.py`` 的 ``BROWSER_HEADERS`` 混进来。
    2. **有频率限制**。短时间连打 10 次后会开始返回 2001 空结果，
       停一会儿自己恢复。所以这里带一次退避重试。

    ⚠️ **字段坑**：返回结构变过，现在歌曲装在
    ``search.data.body.item_song``（**数组**）里，而
    ``search.data.body.song.list`` 恒为空数组。

    xiaofei-plugin 取的是后者（``apps/点歌.js:2250``），所以它的 QQ 音乐
    搜索现在**永远返回空**——这不是接口失效，是取数路径过期了。这里两个
    字段都试，优先 ``item_song``。
    """
    payload = {
        "comm": {"uin": "0", "authst": "", "ct": 29},
        "search": {
            "method": "DoSearchForQQMusicMobile",
            "module": "music.search.SearchCgiService",
            "param": {
                "grp": 1,
                "num_per_page": limit,
                "page_num": 1,
                "query": keyword,
                "remoteplace": "miniapp.1109523715",
                "search_type": 0,
                "searchid": str(_rand_int(1000000, 9999999)),
            },
        },
    }

    data = await _post_qqm(payload, tag="QQ音乐搜索")
    if data is None:
        return []

    body = ((data.get("search") or {}).get("data") or {}).get("body") or {}
    songs = body.get("item_song") or []
    if not songs:
        # 老结构兜底
        songs = ((body.get("song") or {}).get("list")) or []

    out: list[Song] = []
    for item in songs[:limit]:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("mid") or "")
        if not mid:
            continue
        album = item.get("album") or {}
        out.append(
            Song(
                platform="qqmusic",
                song_id=mid,
                name=_strip_em(str(item.get("title") or "")),
                artist="/".join(
                    str(s.get("name", ""))
                    for s in (item.get("singer") or [])
                    if isinstance(s, dict)
                ),
                album=_strip_em(str(album.get("name") or "")),
                cover=_qq_cover(item),
                page_url=QQ_MUSIC_SONG_PAGE.format(mid=mid),
                extra={
                    # 取直链时要用（文件名格式是 M800<media_mid>.mp3）
                    "media_mid": (item.get("file") or {}).get("media_mid"),
                    "file": item.get("file") or {},
                    "pay": item.get("pay") or {},
                },
            )
        )
    return out


async def qqmusic_play_url(
    song: Song, cookie: str = "", high: bool = True
) -> str:
    """取 QQ 音乐音频直链（``CgiGetVkey``）。

    **必须有登录态 Cookie**（含 ``qqmusic_key`` / ``qm_keyst``），否则恒返回
    ``purl`` 为空、``result=104003``。这是实测结论：用固定 mid
    （晴天/稻香/七里香）匿名测试，三首全部取不到。

    ⚠️ **音质档位必须靠 ``filename`` 参数指定**（实测 2026-09-17）：

    =========================  ============================
    传的 filename              实际拿到的
    =========================  ============================
    不传                        ``C400...m4a``（96kbps aac，最低）
    ``M800<media_mid>.mp3``     ``M800...mp3``（320kbps）✅
    ``M500<media_mid>.mp3``     ``M500...mp3``（128kbps）
    ``C400<media_mid>.m4a``     ``C400...m4a``
    =========================  ============================

    也就是**不传 filename 会静默降级到最低音质**，所以这里默认按「320 →
    192ogg → 128 → 96」从高到低挑第一个该曲目有资源的档位。

    参数构造照搬 xiaofei-plugin 的 ``apps/点歌.js:1287-1350``。
    """
    if not cookie.strip():
        logger.info("[R插件] QQ音乐未配置 Cookie，跳过取直链（只会发歌名+链接）")
        return ""

    ck_map = parse_cookie(cookie)
    authst = ck_map.get("qqmusic_key") or ck_map.get("qm_keyst") or ""
    if not authst:
        # 没有 musickey 时 CgiGetVkey 一定失败，早点返回省一次请求
        logger.info("[R插件] QQ音乐 Cookie 里没有 qqmusic_key/qm_keyst，无法取直链")
        return ""

    is_wx = bool(ck_map.get("wxunionid"))
    uin = ck_map.get("wxuin") if is_wx else ck_map.get("uin")
    media_mid = str(song.extra.get("media_mid") or "")
    file_info = song.extra.get("file") or {}

    filenames = _qq_filenames(file_info, media_mid, high)

    # 构造 comm 块：先铺模板，再覆盖登录凭据
    import copy

    payload = copy.deepcopy(QQ_MUSIC_BODY_TEMPLATE)
    comm = payload.setdefault("comm", {})
    comm["uin"] = str(uin or "0")
    comm["authst"] = authst
    comm["tmeLoginType"] = 1 if is_wx else 2
    if is_wx:
        comm["wid"] = str(uin or "0")
        comm["psrf_qqunionid"] = ck_map.get("wxunionid") or ""
    else:
        comm["psrf_qqunionid"] = ck_map.get("psrf_qqunionid") or ""
        comm["psrf_qqopenid"] = ck_map.get("psrf_qqopenid") or ""
    comm["psrf_qqaccess_token"] = ck_map.get("psrf_qqaccess_token") or ""
    comm["guid"] = _md5(str(uin or "000000") + "music")

    req_param: dict[str, Any] = {
        "guid": _md5(str(_now_ms())),
        "songmid": [song.song_id],
        "songtype": [0],
        "uin": str(uin or "0"),
        "loginflag": 1,
        "platform": "20",
    }
    if filenames:
        req_param["filename"] = filenames
    payload["req_0"] = {
        "module": "vkey.GetVkeyServer",
        "method": "CgiGetVkey",
        "param": req_param,
    }

    data = await _post_qqm(
        payload, tag="QQ音乐取直链", cookie=f"qqmusic_key={authst}; uin={uin}"
    )
    if data is None:
        return ""

    req0 = data.get("req_0") or {}
    infos = (req0.get("data") or {}).get("midurlinfo") or []
    sip = (req0.get("data") or {}).get("sip") or []
    base = str(sip[0]).rstrip("/") if sip else "https://ws.stream.qqmusic.qq.com"

    for info in infos:
        if not isinstance(info, dict):
            continue
        purl = str(info.get("purl") or "")
        if purl:
            return f"{base}/{purl}"
        result = info.get("result")
        if result:
            # 104003 = 需要登录/VIP（musickey 过期也表现成这个）
            logger.info(
                f"[R插件] QQ音乐 purl 为空，result={result}"
                f"（104003 通常是 qqmusic_key 过期，有效期约 12 小时）"
            )
    return ""


def _qq_filenames(
    file_info: dict, media_mid: str, high: bool
) -> list[str]:
    """按「高音质优先」拼 ``filename`` 候选列表。

    只挑该曲目 ``file`` 字段里确实有资源的档位——``size_*`` 为 0 或缺失
    说明这个音质不存在，传上去也没用（服务端会跳过）。

    ``high=False`` 时只给 128kbps，省流量（移动网络下的配置项）。
    """
    if not media_mid:
        return []

    tiers = (
        ("size_320mp3", "M800", "mp3"),
        ("size_192ogg", "O600", "ogg"),
        ("size_128mp3", "M500", "mp3"),
        ("size_96aac", "C400", "m4a"),
    )
    if not high:
        tiers = tuple(t for t in tiers if t[0] == "size_128mp3") or tiers

    out: list[str] = []
    for size_key, prefix, ext in tiers:
        try:
            if int(file_info.get(size_key) or 0) < 1:
                continue
        except (TypeError, ValueError):
            continue
        out.append(f"{prefix}{media_mid}.{ext}")
    return out


def _qq_cover(item: dict) -> str:
    """拼 QQ 音乐封面地址。

    照搬 xiaofei-plugin 的优先级：专辑图 > 歌手图。
    ``vs[1]`` 是新版才有的字段（有它说明是高音质版本自己的封面）。
    """
    vs = item.get("vs") or []
    album = item.get("album") or {}
    singers = item.get("singer") or []
    album_mid = str(album.get("mid") or "")
    singer_mid = ""
    if singers and isinstance(singers[0], dict):
        singer_mid = str(singers[0].get("mid") or "")

    if len(vs) > 1 and vs[1]:
        pic = f"T062R150x150M000{vs[1]}"
    elif album_mid:
        pic = f"T002R150x150M000{album_mid}"
    elif singer_mid:
        pic = f"T001R150x150M000{singer_mid}"
    else:
        return ""
    return f"https://y.gtimg.cn/music/photo_new/{pic}.jpg"


# ==========================================================================
# 统一入口
# ==========================================================================


async def search(
    keyword: str, platform: str | None = None, limit: int = MAX_RESULTS
) -> tuple[list[Song], str]:
    """搜索歌曲，返回 ``(结果列表, 平台 key)``。

    ``platform`` 给定时只搜那一个平台；为 ``None`` 时按
    :data:`DEFAULT_ORDER` 依次尝试，**第一个有结果的平台即返回**——
    不然把两个平台的歌混在一张列表里，序号会让人分不清是哪个平台的。
    """
    keyword = keyword.strip()
    if not keyword:
        return [], ""

    if platform:
        key = PLATFORM_ALIASES.get(platform, platform)
        songs = await _search_one(key, keyword, limit)
        return songs, key

    for key in DEFAULT_ORDER:
        songs = await _search_one(key, keyword, limit)
        if songs:
            return songs, key
    return [], ""


async def _search_one(key: str, keyword: str, limit: int) -> list[Song]:
    """带缓存的单平台搜索。

    缓存命中能显著降低 QQ 音乐的限流风险（见 :data:`_SEARCH_CACHE` 说明）。
    ``limit`` 不同视为不同缓存条目——避免「先要 5 条后要 10 条」拿到不够的
    结果。缓存里存全量结果，取用时才切片，这样小 limit 请求不会污染大 limit。
    """
    import time as _time

    cache_key = (key, keyword)
    hit = _SEARCH_CACHE.get(cache_key)
    if hit and (_time.time() - hit[0]) < _SEARCH_CACHE_TTL:
        return hit[1][:limit]

    songs = await _search_one_raw(key, keyword, max(limit, MAX_RESULTS))
    if songs:
        _SEARCH_CACHE[cache_key] = (_time.time(), songs)
    return songs[:limit]


async def _search_one_raw(key: str, keyword: str, limit: int) -> list[Song]:
    if key == "netease":
        return await search_netease(keyword, limit)
    if key == "qqmusic":
        return await search_qqmusic(keyword, limit)
    logger.warning(f"[R插件] 未知的点歌平台: {key}")
    return []


async def get_play_url(
    song: Song,
    netease_cookie: str = "",
    qq_cookie: str = "",
    high: bool = True,
) -> str:
    """按需取音频直链。取不到返回空串（调用方落回 ``page_url``）。"""
    if song.platform == "netease":
        return await netease_play_url(song, netease_cookie, DEFAULT_LEVEL)
    if song.platform == "qqmusic":
        return await qqmusic_play_url(song, qq_cookie, high)
    return ""


# ==========================================================================
# 小工具
# ==========================================================================

# Cookie 字段别名 -> 接口要求的名字。
#
# QQ 音乐后台（``y.qq.com``）在浏览器里存的 Cookie 名，与接口参数名不一致。
# 实测确认的对应关系（2026-09-17）：
#   uid -> uin、qqopenid -> psrf_qqopenid、qqyunionid -> psrf_qqunionid、
#   qqaccess_token -> psrf_qqaccess_token
# 这样用户从浏览器整串复制过来就能直接用，不用自己改名。
_COOKIE_ALIASES: dict[str, str] = {
    "uid": "uin",
    "qqopenid": "psrf_qqopenid",
    "qqyunionid": "psrf_qqunionid",
    "qqaccess_token": "psrf_qqaccess_token",
    "qqrefresh_token": "psrf_qqrefresh_token",
    "qm_keyst": "qqmusic_key",
    "music_key": "qqmusic_key",
    "musicid": "uin",
    "str_musicid": "uin",
    # 微信登录侧的对应名字
    "openid": "wxopenid",
    "unionid": "wxunionid",
    "refresh_token": "wxrefresh_token",
    "access_token": "wxaccess_token",
}


def parse_cookie(raw: str) -> dict[str, str]:
    """把 Cookie 串解析成 dict，并做字段别名映射。

    注意值里可能含 ``=``（base64 常见），所以只按**第一个** ``=`` 切分。
    别名映射见 :data:`_COOKIE_ALIASES`：``uid``/``qqopenid`` 这类浏览器里
    的名字会被转成接口要求的 ``uin``/``psrf_qqopenid``。

    已有的标准名不会被别名覆盖（标准名优先）。
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        out[key] = value

    # 别名补位：标准名已在就直接用，否则看别名
    for alias, standard in _COOKIE_ALIASES.items():
        if alias in out and not out.get(standard):
            out[standard] = out[alias]
    return out


def _strip_em(text: str) -> str:
    """去掉搜索结果里的 ``<em>`` 高亮标签。"""
    return re.sub(r"</?em>", "", text).strip()


def looks_like_audio(body: bytes) -> bool:
    """判断响应体开头像不像音频文件。

    用于**验证**取到的直链是否真的能下载（测试脚本和排障用）。

    实测涉及四种容器格式，所以比只认 mp3 要宽一些：
    - ``ID3``：带 ID3 标签的 mp3（QQ 音乐 M800/M500 是这种）
    - ``0xFF 0xEx``：无标签 mp3 的 MPEG 帧同步字
    - ``[4字节长度]ftyp``：m4a/mp4（QQ 音乐 C400 是 ``ftypmp42``）
    - ``fLaC`` / ``OggS``：flac / ogg
    """
    if len(body) < 64:
        return False
    if body[:3] == b"ID3":
        return True
    if body[0] == 0xFF and (body[1] & 0xE0) == 0xE0:
        return True
    if body[4:8] == b"ftyp":
        return True
    if body[:4] in (b"fLaC", b"OggS"):
        return True
    return False


def _urlencode(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _md5(text: str) -> str:
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _rand_int(low: int, high: int) -> int:
    import random

    return random.randint(low, high)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


async def _post_form(
    url: str, body: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """POST 表单并解析 JSON 响应。

    ``core/http.py`` 只有 ``post_json``（发 JSON 体）；网易云这两个接口
    收的是 ``application/x-www-form-urlencoded``，所以在这里单独实现。
    """
    import json as _json

    from .http import get_session

    merged = {
        "User-Agent": COMMON_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if headers:
        merged.update(headers)

    session = get_session()
    try:
        async with session.post(url, data=body, headers=merged) as resp:
            text = await resp.text()
    except Exception as exc:  # noqa: BLE001
        raise HttpError(f"请求失败: {url} -> {exc}") from exc

    return _loads(text, url)


async def _post_json(
    url: str, payload: Any, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """POST 一个 JSON 体并解析响应（通用，会合并调用方给的头）。"""
    import json as _json

    from .http import get_session

    merged = {"User-Agent": COMMON_USER_AGENT, "Content-Type": "application/json"}
    if headers:
        merged.update(headers)

    session = get_session()
    try:
        async with session.post(
            url, data=_json.dumps(payload, ensure_ascii=False), headers=merged
        ) as resp:
            text = await resp.text()
    except Exception as exc:  # noqa: BLE001
        raise HttpError(f"请求失败: {url} -> {exc}") from exc

    return _loads(text, url)


# QQ 音乐接口专用的「干净头」。
#
# ⚠️ **绝对不能加 ``Accept-Language``**：实测（2026-09-17，逐项隔离）带上它
# 接口固定返回 ``search.code=2001`` + 空 body（2134 字节）；不带则正常
# 返回 24–49KB。UA / Content-Type / Referer / Origin / Accept 都无影响。
_QQM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)"
    ),
    "Content-Type": "application/json",
    # 显式声明不要压缩，避免中间层因编码差异改行为
    "Accept-Encoding": "identity",
}

# QQ 音乐接口的节流锁 + 上次请求时间。
#
# ⚠️ 限流的真实规律（2026-09-17 对照实验确认）：
#
# 同一个请求**连续发多次，结果随机** —— 有成功有失败，且与连接方式
# （urllib / aiohttp、keep-alive 有无）、header、Cookie 都**无关**：
#
# ================================  ==========================
# 测试条件                           结果
# ================================  ==========================
# urllib 连发 3 次                    ✅ / ❌ / ❌
# aiohttp 每次新建 session 连发 3 次   ❌ / ❌ / ❌
# aiohttp 复用 session 连发 3 次       ❌ / ✅ / ✅
# ================================  ==========================
#
# 判断是**服务端多节点、部分节点限流**（负载均衡打到不同后端，行为不一致）。
# 结论：单纯降低频率没用，**重试才是有效手段**。所以下面重试次数给够、
# 退避不要太长（快速换节点比等更有效）。
_QQM_LOCK: asyncio.Lock | None = None
_QQM_LAST = 0.0
_QQM_MIN_INTERVAL = 1.5  # 秒。仅为不主动制造压力，不是限流主因
_QQM_RETRIES = 3  # 重试次数。实测单次成功率约 40%，3 次能把失败率压到 ~20%

# 搜索缓存：``(平台, 关键词)` -> (时间戳, 结果列表)
#
# 这是对抗限流最有效的一招：搜索成功一次就缓存 10 分钟，期间同关键词
# 直接返回，既不打扰接口也不会让用户看到限流失败。
_SEARCH_CACHE: dict[tuple[str, str], tuple[float, list["Song"]]] = {}
_SEARCH_CACHE_TTL = 600.0  # 秒


def clear_search_cache() -> None:
    """清空搜索缓存（测试用）。"""
    _SEARCH_CACHE.clear()


async def _post_qqm(
    payload: Any, *, tag: str, cookie: str = "", retries: int | None = None
) -> dict[str, Any] | None:
    """请求 ``u.y.qq.com/cgi-bin/musicu.fcg``，带节流 + 限流重试。

    返回 ``None`` 表示最终失败（调用方按「没拿到数据」处理）。

    ``cookie`` 只在需要登录态的接口（取直链）传；搜索不用传。

    **重试是核心**：实测该接口的 2001 拒绝有随机性（见 :data:`_QQM_LOCK`
    附近的说明），所以这里默认重试 :data:`_QQM_RETRIES` 次，退避较短
    （换节点比等待更有效）。
    """
    import json as _json

    from .http import get_session

    global _QQM_LOCK, _QQM_LAST

    if retries is None:
        retries = _QQM_RETRIES
    if _QQM_LOCK is None:
        _QQM_LOCK = asyncio.Lock()

    headers = dict(_QQM_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    body = _json.dumps(payload, ensure_ascii=False)
    session = get_session()
    loop = asyncio.get_running_loop()

    async with _QQM_LOCK:
        for attempt in range(retries + 1):
            # 节流：距上次请求不足最小间隔就等一下
            gap = _QQM_MIN_INTERVAL - (loop.time() - _QQM_LAST)
            if gap > 0:
                await asyncio.sleep(gap)

            try:
                async with session.post(
                    QQ_MUSIC_VKEY_API, data=body, headers=headers
                ) as resp:
                    text = await resp.text()
                _QQM_LAST = loop.time()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] {tag} 请求异常: {type(exc).__name__}: {exc}")
                _QQM_LAST = loop.time()
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                return None

            try:
                data = _loads(text, QQ_MUSIC_VKEY_API)
            except HttpError as exc:
                logger.warning(f"[R插件] {tag} 响应解析失败: {exc}")
                return None

            if not _qqm_rate_limited(data):
                if attempt:
                    logger.info(f"[R插件] {tag} 第 {attempt + 1} 次尝试成功")
                return data

            # 2001 是实测的随机拒绝码，重试通常能拿到正常响应
            if attempt < retries:
                logger.info(
                    f"[R插件] {tag} 被限流（code=2001），重试 {attempt + 1}/{retries}"
                )
                await asyncio.sleep(1.5 * (attempt + 1))
            else:
                logger.warning(f"[R插件] {tag} 重试 {retries} 次仍被限流，放弃")

    return None


def _qqm_rate_limited(data: dict) -> bool:
    """判断响应是不是限流空结果。

    特征：``search.code`` 或 ``req_0.code`` 为 2001，且对应 data.body
    是空的。只看 2001 就够——正常请求不会返回这个码。
    """
    for key in ("search", "req_0"):
        node = data.get(key)
        if isinstance(node, dict) and node.get("code") == 2001:
            return True
    return False


def _loads(text: str, url: str) -> dict[str, Any]:
    """容错 JSON 解析（接口偶尔会带 BOM 或前后缀垃圾）。"""
    import json as _json

    text = text.strip()
    if not text:
        raise HttpError(f"接口返回空内容: {url}")
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return _json.loads(text[start : end + 1])
            except _json.JSONDecodeError:
                pass
        raise HttpError(f"接口返回非 JSON: {text[:120]}")
