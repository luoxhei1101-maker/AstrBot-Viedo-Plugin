"""统一异步 HTTP 封装。

对应原版的 utils/common.js（retryAxiosReq / urlTransformShortLink / testProxy）
和散落在各处的裸 fetch 调用。

AstrBot 官方规范要求插件不要用 `requests`，这里统一用 aiohttp —— 它是
AstrBot 本体的核心依赖，插件里不用再单独装。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from astrbot.api import logger

# 原版 general-link-adapter.js 里硬编码的那个 Mac Chrome UA，照搬
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
)

# 原版解析接口那一段用了一整套伪装头，直接抄过来，免得某些接口挑 header
BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": DEFAULT_UA,
}


class HttpError(RuntimeError):
    """网络层错误，方便上层统一兜底。"""


async def fetch(
    url: str,
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> tuple[bytes, str]:
    """GET 一个 URL，自动跟随重定向。

    Returns:
        ``(body_bytes, final_url)`` —— 第二个值是重定向结束后的真实地址，
        短链展开时会用到。
    """
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=connector,
            ) as session:
                async with session.get(url, headers=merged, allow_redirects=True) as resp:
                    body = await resp.read()
                    return body, str(resp.url)
        except Exception as exc:  # noqa: BLE001 - 网络错误种类多，统一重试
            last_err = exc

            # 域名根本解析不出来的时候重试是纯浪费——第三方接口失效最常见的形式
            # 就是域名过期，实测几个接口挂掉后全是 DNS 失败。直接快速失败，
            # 把时间让给下一个接口。
            if _is_dns_error(exc):
                raise HttpError(f"域名解析失败（接口可能已失效）: {url}") from exc

            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue

    raise HttpError(f"请求失败: {url} -> {last_err}") from last_err


async def fetch_head_info(
    url: str,
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    range_bytes: str | None = None,
    retries: int = 1,
) -> tuple[int, dict[str, str], str, str]:
    """只取响应头（可选只下前几个字节），不读完整响应体。

    抖音的画质探测就是靠这个：对同一个视频 ID 试不同 ``ratio``，比较
    ``Content-Range`` / ``Content-Length`` 判断这个档位是否真实存在。
    完整下载一遍太贵，用 ``Range: bytes=0-1`` 把一个字节的响应拉回来即可。

    Returns:
        ``(status, headers_lowercase_keys, set_cookie, final_url)``

        ``set_cookie`` 是 ``Set-Cookie`` 响应头的原始值（多条用 ``\\n`` 连接），
        ttwid 注册要用。
    """
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)
    if range_bytes:
        merged["Range"] = range_bytes

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=connector,
            ) as session:
                async with session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    # 头拿到了就够了，响应体直接丢弃——aiohttp 会在退出上下文时释放
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    set_cookie = "\n".join(resp.headers.getall("Set-Cookie", []))
                    return resp.status, hdrs, set_cookie, str(resp.url)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if _is_dns_error(exc):
                raise HttpError(f"域名解析失败（接口可能已失效）: {url}") from exc
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue

    raise HttpError(f"请求失败: {url} -> {last_err}") from last_err


async def post_json(
    url: str,
    payload: Any,
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    retries: int = 1,
) -> tuple[Any, str]:
    """POST 一个 JSON，返回 ``(解析后的响应体, Set-Cookie 原始串)``。

    用于抖音匿名 ttwid 注册（``ttwid.bytedance.com``）——原版也是纯协议
    POST，不依赖任何登录态。
    """
    merged = dict(BROWSER_HEADERS)
    merged["Content-Type"] = "application/json"
    if headers:
        merged.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=connector,
            ) as session:
                async with session.post(url, json=payload, headers=merged) as resp:
                    set_cookie = "\n".join(resp.headers.getall("Set-Cookie", []))
                    text = await resp.text()
                    try:
                        return json.loads(text), set_cookie
                    except json.JSONDecodeError:
                        return {"_raw": text[:500]}, set_cookie
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if _is_dns_error(exc):
                raise HttpError(f"域名解析失败: {url}") from exc
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue

    raise HttpError(f"请求失败: {url} -> {last_err}") from last_err


def _is_dns_error(exc: BaseException) -> bool:
    """判断是不是 DNS 解析类错误。"""
    name = type(exc).__name__
    if "DNSError" in name or "NameResolution" in name:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "getaddrinfo failed",
            "no address associated with hostname",
        )
    )


async def fetch_json(
    url: str,
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    """GET 一个 URL 并解析成 JSON。

    第三方解析接口返回的 JSON 偶尔带 BOM 或前后缀垃圾，这里做了容错解析。
    """
    body, _ = await fetch(url, timeout=timeout, headers=headers, retries=retries)
    text = body.decode("utf-8", errors="ignore").strip()
    if not text:
        raise HttpError(f"接口返回空内容: {url}")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 有些野接口会在 JSON 外面套一层，退而求其次截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise HttpError(f"接口返回非 JSON: {text[:120]}") from exc
        raise HttpError(f"接口返回非 JSON: {text[:120]}")


async def expand_short_url(url: str, *, timeout: float = 10.0) -> str:
    """展开短链，拿到最终跳转地址。

    对应原版 GeneralLinkAdapter.fetchUrl(url, includeRedirect=True)。
    """
    try:
        _, final_url = await fetch(url, timeout=timeout, retries=1)
        return final_url or url
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件移植版] 短链展开失败，沿用原链接: {exc}")
        return url


async def download_bytes(url: str, *, timeout: float = 60.0) -> bytes:
    """下载二进制内容（图片/视频）。"""
    body, _ = await fetch(url, timeout=timeout, retries=1)
    if not body:
        raise HttpError(f"下载到空内容: {url}")
    return body
