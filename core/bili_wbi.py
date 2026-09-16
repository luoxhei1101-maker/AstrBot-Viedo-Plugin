"""B 站 WBI 签名。

B 站从 2023 年起给绝大部分 web 接口加了 WBI 签名校验，不签名的话
``x/player/wbi/playurl`` 直接返回 -403。

算法本身是公开的、纯计算的（不涉及加密库），流程：

1. 请求 ``x/web-interface/nav``，从 ``data.wbi_img`` 拿到 ``img_url`` / ``sub_url``
2. 从两个 URL 里各取出文件名（去扩展名）作为 ``img_key`` / ``sub_key``
3. 拼起来按固定的重排表打乱，取前 32 位得到 ``mixin_key``
4. 请求参数加上 ``wts``（当前秒级时间戳），按 key 字典序排序，
   值里过滤掉 ``!'()*`` 这几个字符
5. ``w_rid = md5(排序后的 query + mixin_key)``

参数里带上 ``wts`` 和 ``w_rid`` 就算签好了。

注意 ``mixin_key`` 每天会变，所以要对 nav 的返回值做缓存，不能每次请求都去拉。
"""

from __future__ import annotations

import time
from hashlib import md5
from urllib.parse import urlencode

from astrbot.api import logger

from .http import HttpError, fetch_json

# 官方的重排表，固定不变
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# 值里这几个字符会被 B 站过滤掉
_FILTER_CHARS = "!'()*"

_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# 缓存：mixin_key + 拉取时间。按天过期，避免签名过期导致的 403。
_cache: dict[str, object] = {"key": None, "ts": 0.0}
_CACHE_TTL = 12 * 3600


def get_mixin_key(orig: str) -> str:
    """按重排表打乱并取前 32 位。"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _extract_key(url: str) -> str:
    """从 wbi_img 的 URL 里取出文件名（去扩展名）。"""
    return url.rsplit("/", 1)[-1].split(".")[0]


async def fetch_wbi_keys(*, headers: dict | None = None, force: bool = False) -> tuple[str, str]:
    """拉取 img_key / sub_key，带缓存。

    这两个 key 是 nav 接口返回的，未登录也能拿到，所以签名本身不需要 Cookie。
    """
    now = time.time()
    cached = _cache.get("key")
    if not force and cached and (now - float(_cache["ts"])) < _CACHE_TTL:
        return cached  # type: ignore[return-value]

    try:
        data = await fetch_json(_NAV_URL, headers=headers, retries=1)
    except HttpError as exc:
        raise HttpError(f"获取 WBI 签名密钥失败: {exc}") from exc

    wbi_img = ((data.get("data") or {}).get("wbi_img")) or {}
    img_url = wbi_img.get("img_url") or ""
    sub_url = wbi_img.get("sub_url") or ""

    if not img_url or not sub_url:
        raise HttpError("nav 接口没有返回 wbi_img，无法签名")

    img_key = _extract_key(img_url)
    sub_key = _extract_key(sub_url)

    _cache["key"] = (img_key, sub_key)
    _cache["ts"] = now
    logger.debug(f"[R插件][B站] WBI 密钥已刷新: img_key={img_key[:8]}... sub_key={sub_key[:8]}...")
    return img_key, sub_key


def sign_params(params: dict, img_key: str, sub_key: str) -> dict:
    """给参数签名，返回带 ``wts`` 和 ``w_rid`` 的新字典。"""
    mixin_key = get_mixin_key(img_key + sub_key)

    signed = {k: v for k, v in params.items()}
    signed["wts"] = int(time.time())

    # 按 key 字典序排序
    signed = dict(sorted(signed.items()))

    # 值里的特殊字符要过滤，且统一转成字符串
    cleaned = {
        k: "".join(ch for ch in str(v) if ch not in _FILTER_CHARS)
        for k, v in signed.items()
    }

    query = urlencode(cleaned)
    cleaned["w_rid"] = md5((query + mixin_key).encode()).hexdigest()
    return cleaned


async def signed_get_json(
    base_url: str,
    params: dict,
    *,
    headers: dict | None = None,
    retries: int = 1,
) -> dict:
    """带 WBI 签名地请求一个 B 站接口。

    如果返回 -403（签名过期），会自动强制刷新一次密钥后重试。
    """
    img_key, sub_key = await fetch_wbi_keys(headers=headers)
    signed = sign_params(params, img_key, sub_key)
    url = f"{base_url}?{urlencode(signed)}"

    data = await fetch_json(url, headers=headers, retries=retries)

    if data.get("code") == -403:
        logger.info("[R插件][B站] 签名为 -403，强制刷新密钥后重试一次")
        img_key, sub_key = await fetch_wbi_keys(headers=headers, force=True)
        signed = sign_params(params, img_key, sub_key)
        url = f"{base_url}?{urlencode(signed)}"
        data = await fetch_json(url, headers=headers, retries=retries)

    return data
