"""小红书笔记解析。

走的是**网页 HTML**（``window.__INITIAL_STATE__``），不是 API —— 所以不需要
``x-s`` 签名，只需要一个有效的登录态 Cookie。

实测要点（2026-09-18，服务器真实 Cookie，每种组合打 4 次，结果完全一致）
=======================================================================

**① Cookie 只能带 ``web_session``（可选加 ``a1``）—— 带上 ``webId`` 必然失败。**

这个结论反直觉，但是实测得很死：

======================  =========  ==========
Cookie 组合             HTML 长度   结果
======================  =========  ==========
完整（17 字段）          101903     失败
``a1+web_session+webId`` 101903     失败
``web_session+webId``    101903     失败
只 ``web_session``        74874      **成功，拿到笔记**
``a1+web_session``        74874      成功
``web_session+xsecappid`` 74874      成功
匿名                     35871      失败（无内容）
======================  =========  ==========

带 ``webId`` 时服务端返回的是一个 101903 字节的**验证页**（里面连
``__INITIAL_STATE__`` 都不是合法 JSON），而不是正常的 74874 字节内容页。

> 原版是「整串透传」，所以它在现在会失败 —— 这不是「少传省事」，
> 是必须做的裁剪。见 :func:`slim_cookie`。

**② 匿名完全不可用**：会被 302 到 ``/login?redirectPath=...``，页面里
``noteDetailMap`` 与 ``feed.feeds`` 都是空的。Cookie 是硬门槛。

**③ 短链跳转后的 URL 带 ``xsec_token`` + ``xsec_source``**，这两个是取内容的
前提；``xsec_token`` 与链接绑定且有时效，所以**不能用通用链接代替真实分享链接**。

**④ ``__INITIAL_STATE__`` 必须先把 ``undefined`` 替换成 ``null``** 才能
``json.loads``（JS 字面量不是合法 JSON）。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

from ..core.constants import XHS_REQ_LINK, XHS_VIDEO
from ..core.http import get_session
from .base import ResolveResult, ResolverContext, register

# ==========================================================================
# 常量
# ==========================================================================

# 原版用的固定 UA（XHS_NO_WATERMARK_HEADER）。小红书对 UA 敏感，
# 这里保留原值而不是用项目通用的 BROWSER_HEADERS。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
)
_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9"
)

# 手机短链（xhslink.com / xhslink.cn）与 PC 链接
_LINK_RE = re.compile(
    r"https?://(?:xhslink\.(?:com|cn)|(?:www\.)?xiaohongshu\.com)"
    r"/[A-Za-z\d._?%&+\-=\/#@]*",
    re.I,
)
_ID_RE = re.compile(r"/(?:explore|item|discovery/item)/([0-9a-fA-F]+)", re.I)
_STATE_RE = re.compile(r"window\.__INITIAL_STATE__=(.*?)</script>", re.S)

# 必需：登录态凭据，少了它服务端当匿名处理（一定没有内容）
_REQUIRED_KEY = "web_session"
# 白名单里额外保留的字段。实测这些都无害；关键是**不能有 webId**。
_KEEP_KEYS = ("web_session", "a1", "gid", "xsecappid", "webBuild", "abRequestId")
# 「有害字段」只放有依据的：
# - ``webid``：实测一票否决（带上必失败）
# - ``last_web_session``：上一轮会话凭据，与当前 ``web_session`` 并存易冲突
# - ``id_token``：另一套凭据，同样可能引起状态不一致
# 其余风控标记（websectiga / sec_poison_id / acw_tc）没有证据有害，
# 兜底方案里保留它们更保险。
_DROP_KEYS = ("webid", "id_token", "last_web_session")

_TIMEOUT = 25.0


# ==========================================================================
# Cookie 处理
# ==========================================================================


def cookie_map(raw: str) -> dict[str, str]:
    """Cookie 串 -> dict（按第一个 ``=`` 切，值里可能有 ``=``）。"""
    out: dict[str, str] = {}
    for part in (raw or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def slim_cookie(raw: str) -> str:
    """裁出可用的 Cookie 字段。

    **必须剔除 ``webId``** —— 带上它会稳定拿到验证页而不是内容页
    （见模块 docstring 的对照表）。这不是可选的优化。
    """
    m = cookie_map(raw)
    if not m.get(_REQUIRED_KEY):
        return ""
    keep = [k for k in _KEEP_KEYS if k in m]
    picked = [f"{k}={m[k]}" for k in keep]
    dropped = [k for k in m if k not in _KEEP_KEYS]
    if dropped:
        logger.debug(f"[R插件] 小红书 Cookie 裁剪掉 {len(dropped)} 个字段: {dropped}")
    return "; ".join(picked)


def full_cookie_without_drop(raw: str) -> str:
    """整串去掉已知有害字段（``webId`` 等），保留其余。

    作为 :func:`slim_cookie` 的兜底：万一哪天白名单太窄导致失败，
    至少还能试「保留用户所有字段、只剔除元凶」这一版。
    """
    m = cookie_map(raw)
    if not m.get(_REQUIRED_KEY):
        return ""
    out = [f"{k}={v}" for k, v in m.items() if k.lower() not in _DROP_KEYS]
    return "; ".join(out)


# ==========================================================================
# HTTP
# ==========================================================================


async def _get(url: str, cookie: str) -> tuple[str, str]:
    """GET 一个页面，返回 ``(html, 最终URL)``。"""
    headers = {
        "accept": _ACCEPT,
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    session = get_session()
    async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
        body = await resp.read()
        return body.decode("utf-8", errors="ignore"), str(resp.url)


def _params_from_url(url: str) -> tuple[str | None, str | None, str]:
    """从链接里取 ``(note_id, xsec_token, xsec_source)``。

    短链跳到 captcha 页时，真正的参数在 ``redirectPath`` 里 —— 原版专门处理了
    这个分支，实测确实需要。
    """
    parsed = urlparse(url)
    id_match = _ID_RE.search(parsed.path)
    note_id = id_match.group(1) if id_match else None
    qs = parse_qs(parsed.query)
    token = (qs.get("xsec_token") or [None])[0]
    source = (qs.get("xsec_source") or [""])[0]

    # captcha 兜底：redirectPath 里可能藏着完整参数
    if (not note_id or not token) and "captcha" in parsed.path:
        redirect = (qs.get("redirectPath") or [None])[0]
        if redirect:
            inner = urlparse(redirect)
            if not note_id:
                inner_match = _ID_RE.search(inner.path)
                note_id = inner_match.group(1) if inner_match else None
            iqs = parse_qs(inner.query)
            token = token or (iqs.get("xsec_token") or [None])[0]
            source = source or (iqs.get("xsec_source") or [""])[0]

    return note_id, token, source or "pc_feed"


def _parse_state(html: str) -> dict | None:
    """解出 ``__INITIAL_STATE__``。"""
    m = _STATE_RE.search(html)
    if not m:
        return None
    raw = m.group(1)
    for attempt in (
        lambda s: s.replace("undefined", "null"),
        # 兜底：JS 里还可能有 NaN / Infinity，JSON 都不认
        lambda s: s.replace("undefined", "null").replace("NaN", "null")
        .replace("Infinity", "null"),
    ):
        try:
            data = json.loads(attempt(raw))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _pick_note(state: dict, note_id: str) -> dict | None:
    """从 state 里取笔记正文。"""
    ndm = ((state.get("note") or {}).get("noteDetailMap") or {})
    if not isinstance(ndm, dict):
        return None
    # 优先按 id 取；键名偶尔带后缀，退而求其次取第一个有 note 的
    entry = ndm.get(note_id)
    if isinstance(entry, dict) and entry.get("note"):
        return entry["note"]
    for v in ndm.values():
        if isinstance(v, dict) and v.get("note"):
            return v["note"]
    return None


def _strip_topic_marks(text: str) -> str:
    """把小红书的 ``#罗小黑[话题]#`` 规整成 ``#罗小黑#``。

    网页源码里话题是 ``#名字[话题]#`` 这种带后缀的写法，直接显示会多出
    ``[话题]#`` 这几个字。
    """
    return re.sub(r"#([^\s#\[\]]+)\[话题\]#", r"#\1#", text or "")


def _extract_media(note: dict) -> tuple[list[str], list[str]]:
    """返回 ``(图片URL列表, 视频URL列表)``。

    图片：``imageList[].urlDefault``。

    视频：**不能写死 ``stream.h264``** —— 小红书现在用编码器代号做键
    （``EF4``/``EF5``/``EF6``/``EF7``，对应 streamType 259/301/309/76…），
    原版的 ``stream.h264?.[0]?.masterUrl`` 因此取不到地址。这里遍历
    ``stream`` 的所有键，按「分辨率 → 码率」降序返回候选，让下载层自己
    挑（CDN 偶发单条不可用，多候选更稳）。
    """
    images: list[str] = []
    for item in note.get("imageList") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("urlDefault") or item.get("url") or "").strip()
        if url.startswith("http"):
            images.append(url)

    video = note.get("video") or {}
    streams = ((video.get("media") or {}).get("stream") or {})

    def _int(value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    ranked: list[tuple[int, int, str]] = []
    for entries in streams.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("masterUrl") or "").strip()
            if not url.startswith("http"):
                continue
            # 用「总像素」排序，横屏 / 竖屏都能正确比较；
            # ⚠️ 不要按 streamType 排 —— 它和分辨率没有单调关系：
            # 实测同一条笔记里 76→1800x1440、108→2560x1440、259→900x720、
            # 301→1350x1080、309→900x720，数字小的反而更清晰。
            pixels = _int(entry.get("width")) * _int(entry.get("height"))
            ranked.append((pixels, _int(entry.get("videoBitrate")), url))

    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    videos = [u for _, _, u in ranked]

    # 没有 masterUrl 时退回「无水印」拼法（``originVideoKey``）。
    # 原版把这个方案注释掉了、只用 masterUrl，这里作为兜底保留。
    if not videos:
        key = str((video.get("consumer") or {}).get("originVideoKey") or "").strip()
        if key:
            videos.append(f"{XHS_VIDEO.rstrip('/')}/{key.lstrip('/')}")

    return images, videos


# ==========================================================================
# 主入口
# ==========================================================================


@register("xiaohongshu")
async def resolve_xiaohongshu(link: str, ctx: ResolverContext) -> ResolveResult:
    """小红书图文 / 视频笔记。"""
    raw_cookie = ctx.cookie("xiaohongshu") or ""
    if not raw_cookie.strip():
        return ResolveResult.fail(
            "小红书",
            "未配置小红书 Cookie。小红书现在必须登录态才能取内容"
            "（配置项：其他平台 → 小红书的Cookie）",
        )

    slim = slim_cookie(raw_cookie)
    if not slim:
        return ResolveResult.fail(
            "小红书",
            "Cookie 里没有 web_session（登录态凭据）。"
            "请从已登录的浏览器重新复制整串 Cookie",
        )

    m = _LINK_RE.search(link)
    if not m:
        return ResolveResult.fail("小红书", "链接里没有识别到小红书地址")
    url = m.group(0).replace("&amp;", "&")

    # ---------------------------------------------------------------- 展开短链
    note_id: str | None = None
    token: str | None = None
    source = "pc_feed"
    final_url = url
    try:
        if "xhslink" in urlparse(url).netloc.lower():
            html, final_url = await _get(url, slim)
            logger.info(f"[R插件] 小红书短链跳转: {final_url[:120]}")
            note_id, token, source = _params_from_url(final_url)
        else:
            note_id, token, source = _params_from_url(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 小红书短链展开失败: {type(exc).__name__}: {exc}")
        return ResolveResult.fail("小红书", f"短链展开失败：{type(exc).__name__}")

    if not note_id:
        return ResolveResult.fail("小红书", "链接里没有笔记 id（可能链接已失效）")
    if not token:
        return ResolveResult.fail(
            "小红书",
            "链接里缺少 xsec_token —— 请用 App 里「分享 → 复制链接」得到的链接，"
            "直接复制浏览器地址栏的链接往往不带这个参数",
        )

    logger.info(
        f"[R插件] 小红书笔记 id={note_id} "
        f"token={'有' if token else '无'} source={source}"
    )

    # ---------------------------------------------------------------- 取内容页
    page = (
        f"{XHS_REQ_LINK.rstrip('/')}/{note_id}"
        f"?xsec_token={token}&xsec_source={source}"
    )

    async def _try(cookie: str) -> tuple[str, str]:
        try:
            return await _get(page, cookie)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件] 小红书取页面失败: {type(exc).__name__}: {exc}")
            return "", ""

    html, _ = await _try(slim)
    note = _pick_note(_parse_state(html) or {}, note_id) if html else None

    # 兜底：万一白名单太窄，用「整串去 webId」再试一次
    if note is None:
        logger.info("[R插件] 小红书白名单 Cookie 没拿到内容，改用「整串去 webId」重试")
        html2, _ = await _try(full_cookie_without_drop(raw_cookie))
        if html2:
            note = _pick_note(_parse_state(html2) or {}, note_id)
            if note is not None:
                html = html2

    if not html:
        return ResolveResult.fail("小红书", "取笔记页面失败（网络或接口异常）")

    if note is None:
        if "__INITIAL_STATE__" not in html:
            return ResolveResult.fail(
                "小红书", "页面里没有 __INITIAL_STATE__，小红书可能改了页面结构"
            )
        if _parse_state(html) is None:
            return ResolveResult.fail(
                "小红书",
                "页面数据解析失败 —— 拿到的是验证页，通常说明 Cookie 失效或被风控",
            )
        return ResolveResult.fail(
            "小红书",
            "没取到笔记内容，可能原因：Cookie 已失效、笔记被删除或需要权限",
        )

    # ---------------------------------------------------------------- 组装
    images, video_candidates = _extract_media(note)
    # ⚠️ ``videos`` 只放**一条**（画质最好的）。多个候选是同一视频的不同
    # 画质档 / CDN，而 main.py 的 `_build_video_chain` 会把 ``videos`` 里的
    # 每一条都发出去（那是为抖音动图设计的）—— 放多个会导致一条笔记发 4 个视频。
    # 其余候选放 extra，仅用于下载失败时重试。
    videos = video_candidates[:1]
    backups = video_candidates[1:]
    raw_title = str(note.get("title") or "").strip()
    desc = _strip_topic_marks(str(note.get("desc") or "").strip())
    author = str((note.get("user") or {}).get("nickname") or "").strip()
    cover = images[0] if images else ""
    note_type = str(note.get("type") or "")

    # 图文笔记通常没有 title，用正文首行兜底 —— 否则回复里标题是空的
    title = raw_title
    if not title and desc:
        title = desc.splitlines()[0].strip()[:30]
    if not title:
        title = "小红书图文" if note_type != "video" else "小红书视频"

    if not videos and not images:
        return ResolveResult.fail("小红书", "这条笔记里没有可下载的图片或视频")

    logger.info(
        f"[R插件] 小红书解析成功 type={note_type} "
        f"图片 {len(images)} 张 视频 {len(videos)} 个"
    )

    return ResolveResult.ok(
        "小红书",
        videos=videos,
        images=images,
        title=title or "小红书笔记",
        author=author,
        desc=desc,
        cover=cover,
        extra={
            "note_type": note_type,
            "note_id": note_id,
            # 同一视频的其余画质档 / 备份 CDN，下载失败时按序重试用
            "video_backups": backups,
        },
    )
