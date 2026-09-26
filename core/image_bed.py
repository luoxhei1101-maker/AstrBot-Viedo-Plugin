"""免费图床：把图片转成一个**唯一的公网直链**。

**为什么需要这一步**（三个约束逼出来的）：

1. QQ markdown 内嵌图要求「公网可访问的 url」，而 ``Comp.Image`` 那条 media 路
   和 markdown **互斥**（适配器会把 markdown 摘掉）—— 所以菜单图必须走 MD 内嵌；
2. **URL 必须每次不同**：同一个 URL 会被客户端缓存，菜单图就永远是同一张；
3. **markdown 内嵌图必须带尺寸**：而随机图 API（``api.elaina.cat``）**只吐图片
   字节、不给 URL**，且每次请求都是**另一张图** —— 插件探到的是 A 的尺寸、
   腾讯去取的是 B，比例对不上就变形（实测 ``/random/`` 的比例极差有 1.08）。

所以流程是：**插件取图 → 本地读真实尺寸 → 上传图床拿唯一 URL → 拼 MD**。
这样 URL 唯一（不缓存）+ 尺寸精确（不变形），两个问题一起解决。

选 freeimage.host 的原因（2026-09-23 实测）：

======================  ==========  ============================================
图床                    结果        备注
======================  ==========  ============================================
**freeimage.host**      ✅ 可用     免注册（文档里有公开测试 key）；**不限时**
Telegraph               ❌          上传返回 "Unknown error"（IP 被限流）
Catbox                  ❌          Invalid uploader
0x0.st                  ❌          官方已关闭上传（"AI botnet spam"）
sm.ms 匿名接口          ❌          空响应（需 token）
======================  ==========  ============================================

**关键验证**：它返回的直链域名是 ``iili.io`` —— 实测**国内可直读**
（`HTTP 200` + `image/jpeg`），腾讯服务器取图没问题。
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger

from .http import get_session

#: freeimage.host 文档里给出的**公开测试 key**。
#: 注册一个免费账号后可以在插件配置（``plugin.imageBedKey``）里换成自己的 ——
#: 公开 key 被限流时会更稳。
DEFAULT_KEY = "6d207e02198a847aa98d0a2a901485a5"

#: 上传接口 + 它返回的直链域名（保底用；实际以响应里的 ``image.url`` 为准）
API = "https://freeimage.host/api/1/upload"
CDN_HOST = "iili.io"


async def upload_image(
    data: bytes,
    *,
    key: str = "",
    filename: str = "image.jpg",
    timeout: float = 30.0,
) -> str:
    """把图片字节传到图床，返回**公网直链**；失败返回空串。

    全程**不抛异常**（图床挂了不该拖垮菜单），失败由调用方降级处理。

    :param data: 图片字节。图床按 5MB 左右为上限，调用方自己控制。
    :param key: 自己的 API key；为空时用公开测试 key。
    :param filename: 上传时的文件名（影响图床给的扩展名）。
    :param timeout: 上传超时（秒）。图只有几百 KB，30 秒很宽裕。
    """
    if not data:
        return ""

    try:
        session = get_session()
        form = aiohttp.FormData()
        form.add_field("key", (key or "").strip() or DEFAULT_KEY)
        form.add_field("action", "upload")
        form.add_field("format", "json")
        form.add_field(
            "source",
            data,
            filename=filename,
            content_type="image/jpeg",
        )
        async with session.post(
            API, data=form, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                logger.debug(f"[R插件][图床] 上传返回 HTTP {resp.status}，跳过")
                return ""
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][图床] 上传失败: {type(exc).__name__}: {exc}")
        return ""

    image = (payload or {}).get("image") or {}
    url = str(image.get("url") or "").strip()
    if not url.startswith("http"):
        logger.debug(f"[R插件][图床] 返回里没有可用 url: {str(payload)[:160]}")
        return ""
    return url



# ======================================================================
# 国内转存（主路径）
# ======================================================================
#
#: czcn 的「URL 转存」接口：给一个图片地址，它下载后落到**自己的国内 OSS**，
#: 返回一个**内容固定**的直链。
#:
#: 为什么它是首选（2026-09-23 实测）：
#:
#: * **节点在国内** —— ``czoss.czcn.xyz`` 解析到 ``113.96.129.5/7/8``（广州电信），
#:   响应头 ``Server: ESA``（阿里云边缘安全加速）；
#: * **内容固定** —— 同一个直链连读两次，字节数完全一致（408680B / 408680B），
#:   所以先转存、再量尺寸，不会像直接嵌随机图 API 那样量到另一张图；
#: * **能直接吃随机图 API 的地址** —— 连「下载字节 + 上传图床」两步都省了。
#:
#: 对比：freeimage.host（``iili.io``）在 Cloudflare 上，实测**广州本地读得到**，
#: 但放进 QQ 官机的 markdown 后客户端报「图片加载失败」—— 官机的图是
#: **腾讯服务器去下载转存**的，海外/CDN 那边取不到就白搭。国内节点才稳。
CZOSS_API = "https://api.czcn.xyz/api/czoss"

# ---------------------------------------------------------------------------
# 「转存必然失败」的 host —— 直接跳过，别再白试（2026-09-26）
# ---------------------------------------------------------------------------
#
# 抖音的签名图 CDN（``p3-pc-sign.douyinpic.com``）**本机 403、czoss 也 403**
# （v1.6.7 实测：换 p3/p6/p9 节点、加 Referer、带 1449 字符 Cookie 全是 403），
# 只有**腾讯自己的下载器**能取到 —— 而官机 markdown 恰恰就是让腾讯去下载。
#
# 对这类 host 调 ``transfer_url`` 是**纯浪费**：
#   3 次尝试 + 2 × 0.8 秒间隔 ≈ 2.4 秒，之后 ``_host_one`` 还会再走一整套
#   「本机下载 → 图床 → 转存」（又是几秒），**最终结果仍然是保留原始直链**。
#   实测官机图集因此要等 4~8 秒才发得出来（2026-09-26 的线上日志）。
#
# 命中这些 host 的图**直接跳过转存**，把原始直链交给腾讯 —— 图集回到 1 秒内。
#
# ⚠️ 往这个名单里加成员要谨慎：走这条路的图**完全依赖腾讯能取到**，
# 加错了的表现是「图集里某张图不显示」。拿不准就让它走转存（慢，但稳）。
SKIP_TRANSFER_HOSTS: tuple[str, ...] = (
    "douyinpic.com",  # 抖音签名图 CDN：p3-pc-sign / p6-pc-sign / p9-pc-sign …
)


def should_skip_transfer(url: str) -> bool:
    """这个 URL 是否属于「转存必然失败、但腾讯能取」那一类。

    见 ``SKIP_TRANSFER_HOSTS`` 的说明。调用方据此**跳过**无谓的转存重试，
    直接把原始直链交给腾讯去下载。
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    # 匹配自身及其子域（p3-pc-sign.douyinpic.com 命中，notdouyinpic.com 不命中）
    return any(host == h or host.endswith("." + h) for h in SKIP_TRANSFER_HOSTS)


async def _transfer_once(src: str, *, key: str, timeout: float) -> str:
    """转存一次（不重试）。失败返回空串。"""
    params: dict[str, str] = {"url": src}
    if (key or "").strip():
        params["key"] = key.strip()

    try:
        session = get_session()
        async with session.get(
            CZOSS_API, params=params, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                logger.debug(f"[R插件][转存] HTTP {resp.status}")
                return ""
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][转存] 请求失败: {type(exc).__name__}: {exc}")
        return ""

    if not isinstance(payload, dict) or payload.get("status") != "success":
        logger.debug(f"[R插件][转存] 返回不正常: {str(payload)[:160]}")
        return ""

    data = payload.get("data") or {}
    url = str(data.get("direct_url") or "").strip()
    if not url.startswith("http"):
        logger.debug(f"[R插件][转存] 没有 direct_url: {str(payload)[:160]}")
        return ""

    # 它默认回 http:// —— markdown 内嵌图实测必须 https 才认。
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    mime = str(data.get("detected_mime") or "")
    if mime and not mime.startswith("image/"):
        logger.debug(f"[R插件][转存] 转存回来的不是图片（{mime}），丢弃")
        return ""

    logger.debug(f"[R插件][转存] OK {data.get('file_size_kb')}KB {mime} -> {url}")
    return url


async def transfer_url(
    src: str, *, key: str = "", timeout: float = 45.0, retries: int = 2
) -> str:
    """把 ``src`` 交给 czoss 转存，返回**固定直链**；失败返回空串。

    :param src: 源图片地址。可以直接是**随机图 API 的地址**（每次吐另一张图）——
        转存后 URL 固定、内容固定，尺寸才量得准；而且 URL 每次不同，
        不会被客户端缓存（菜单图就不会永远是同一张）。
    :param key: 可选 API key（``plugin.czossKey``）。**要用官机菜单图就该填**，
        不填也能跑但更容易被限流。
    :param timeout: 单次转存要它去下载再落盘，给宽一点。
    :param retries: **重试次数**。实测这个接口**偶发失败** ——
        有时干脆不返回，有时下载被截断（只拿到几十 KB）。隔 0.8 秒重试一次
        基本就好。全失败才返回空串，调用方降级走图床。

    全程**不抛异常**（转存挂了不该拖垮菜单）。
    """
    src = (src or "").strip()
    if not src.startswith("http"):
        return ""

    for attempt in range(retries + 1):
        url = await _transfer_once(src, key=key, timeout=timeout)
        if url:
            return url
        if attempt < retries:
            logger.debug(f"[R插件][转存] 第 {attempt + 1} 次没成，重试")
            await asyncio.sleep(0.8)

    logger.warning(
        f"[R插件][转存] 试了 {retries + 1} 次都没成，降级走图床（源={src[:70]}）"
    )
    return ""
