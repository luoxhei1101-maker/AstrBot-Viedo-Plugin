"""统一异步 HTTP 封装。

对应原版的 utils/common.js（retryAxiosReq / urlTransformShortLink / testProxy）
和散落在各处的裸 fetch 调用。

AstrBot 官方规范要求插件不要用 `requests`，这里统一用 aiohttp —— 它是
AstrBot 本体的核心依赖，插件里不用再单独装。

**连接复用（性能关键）**
=======================

早期实现每个请求都 ``TCPConnector(ssl=False)`` + ``ClientSession(...)``，
而且写在 retry 循环**里面**——一次重试就再建一套。代价是：

- 每次请求都重做 TCP 三次握手 + TLS 协商（境外接口 100–300ms）
- 连接池 ``limit`` 形同虚设，keep-alive 完全用不上
- 抖音一次解析要发 1(签名) + 1(主接口) + 4(画质探测) 个请求，
  B 站一次解析要发 3–4 个，全部各建一套

现在改为**模块级 lazy 单例 session**：所有请求共用连接池，
retry 只重试请求本身，不再重建 session。冷启动只付一次建连成本。

**为什么要 lazy 而不是在模块导入时建？**

``ClientSession`` 必须在事件循环里创建。模块导入发生在 AstrBot 启动阶段，
那时未必有运行中的 loop（而且不同事件循环里创建的 session 不能跨用），
所以用 ``get_session()`` 在首次真正发请求时按当前 loop 建。
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


# ==========================================================================
# 共享连接池
# ==========================================================================

# 连接池上限。解析是「突发少量并发」的场景（图集下载另走 downloader.py
# 自己的池），这里给一个够用又不至于泄漏太多 socket 的值。
_POOL_LIMIT = 32
_DEFAULT_TIMEOUT = 30.0

_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None


def _build_session(loop: asyncio.AbstractEventLoop) -> aiohttp.ClientSession:
    """按给定事件循环建一个带连接池的 session。"""
    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=_POOL_LIMIT,
        limit_per_host=_POOL_LIMIT,
        # keep-alive 复用：B 站/抖音都是同一 host 连发多个请求，
        # 复用能省掉重复的 TCP + TLS 握手
        ttl_dns_cache=300,
    )
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT),
        connector=connector,
        # 插件自己管 Cookie（显式塞进 header），不要 session 级别的 jar，
        # 避免跨请求串味（比如抖音的 ttwid 污染 B 站的请求）
        cookie_jar=aiohttp.DummyCookieJar(),
    )


def get_session() -> aiohttp.ClientSession:
    """取共享 session；没有或事件循环换了就重建。

    aiohttp 的 session 绑定创建它的那个事件循环，不能跨 loop 复用。
    插件重载 / 测试里多次 ``asyncio.run`` 都会产生新 loop，所以这里
    检测到 loop 变了就丢弃旧 session 重建，避免 ``RuntimeError: Event loop
    is closed``。
    """
    global _session, _session_loop

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有运行中的 loop，调用方本就不该在同步上下文发请求
        loop = None

    if (
        _session is not None
        and not _session.closed
        and _session_loop is loop
    ):
        return _session

    # loop 变了（或首次）：旧 session 所属的 loop 多半已经关了，
    # 直接丢弃即可，不要在旧 loop 上 await close()
    _session = _build_session(loop)  # type: ignore[arg-type]
    _session_loop = loop
    logger.debug("[R插件][HTTP] 已建立共享连接池")
    return _session


async def close_session() -> None:
    """关闭共享 session。插件卸载时调用，释放连接。"""
    global _session, _session_loop
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不该影响卸载
            logger.debug(f"[R插件][HTTP] 关闭共享连接池出错: {exc}")
    _session = None
    _session_loop = None


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

    session = get_session()
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            # 每个请求单独设 timeout，而不是靠 session 的全局值——
            # 短链展开要快（10s）、媒体下载要慢（120s），共用池也得各自控制
            async with session.get(
                url,
                headers=merged,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
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
    session = get_session()
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                headers=merged,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=timeout),
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
    session = get_session()
    for attempt in range(retries + 1):
        try:
            async with session.post(
                url,
                json=payload,
                headers=merged,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
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


async def fetch_with_cookies(
    url: str,
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> tuple[bytes, str, dict[str, str]]:
    """GET 一个 URL，跟随重定向，并收集重定向链上的所有 Set-Cookie。

    普通 ``fetch`` 只返回响应体，把响应头丢了。但 B 站扫码登录的 SESSDATA
    是 poll 接口在登录成功时通过 ``Set-Cookie`` 响应头下发的，拿不到响应头
    就等于拿不到登录态。这里用 CookieJar 把整条重定向链的 Cookie 都收进来。

    Returns:
        ``(body_bytes, final_url, cookies)`` —— ``cookies`` 是 ``{cookie名: 值}``，
        值保留 URL 编码形式（与浏览器实际发送的一致，例如 SESSDATA 里的
        ``%2C`` 逗号不会被 decode 掉）。
    """
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)

    # 这个函数**不能**用共享 session：它要靠 session 级 CookieJar 收集整条
    # 重定向链上的 Set-Cookie（B 站扫码的 SESSDATA 就在这里下发），而共享
    # session 用的是 DummyCookieJar（故意不存 cookie，避免跨请求串味）。
    #
    # 但连接器可以复用共享池——这样仍然享受 keep-alive，只是多一个轻量的
    # session 外壳（session 本身只是个上下文对象，开销远小于建连接）。
    shared = get_session()
    jar = aiohttp.CookieJar(unsafe=True)
    temp_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
        connector=shared.connector,
        cookie_jar=jar,
    )

    last_err: Exception | None = None
    try:
        for attempt in range(retries + 1):
            try:
                async with temp_session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    body = await resp.read()
                    final_url = str(resp.url)
                    cookies: dict[str, str] = {}
                    for key, morsel in resp.cookies.items():
                        # coded_value 保留 %2C 等编码，和浏览器发出的 cookie 一致
                        try:
                            cookies[key] = morsel.coded_value
                        except AttributeError:  # pragma: no cover - 旧版 aiohttp 兜底
                            cookies[key] = morsel.value
                    return body, final_url, cookies
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if _is_dns_error(exc):
                    raise HttpError(f"域名解析失败（接口可能已失效）: {url}") from exc
                if attempt < retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
    finally:
        # 只关外壳，不关 connector（它是共享的，关了会连累所有请求）
        await temp_session.close()

    raise HttpError(f"请求失败: {url} -> {last_err}") from last_err


async def fetch_json_with_cookies(
    url: str,
    *,
    timeout: float = 15.0,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> tuple[dict[str, Any], dict[str, str]]:
    """GET 一个 URL 解析成 JSON，同时返回响应链上的 Set-Cookie。

    ``fetch_json`` 的增强版，专给 B 站扫码登录这类「登录态在 Set-Cookie 里」
    的接口用。返回 ``(json_dict, cookies)``。
    """
    body, _, cookies = await fetch_with_cookies(
        url, timeout=timeout, headers=headers, retries=retries
    )
    text = body.decode("utf-8", errors="ignore").strip()
    if not text:
        raise HttpError(f"接口返回空内容: {url}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise HttpError(f"接口返回非 JSON: {text[:120]}") from exc
        else:
            raise HttpError(f"接口返回非 JSON: {text[:120]}")
    return data, cookies


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
