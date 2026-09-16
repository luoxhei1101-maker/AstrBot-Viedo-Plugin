"""B 站扫码登录。

移植自原插件 ``apps/tools.js`` 的 ``biliScan`` / ``biliState`` 两个 handler。

流程（B 站官方公开接口，对应 constants/tools.js 的两个常量）：

1. ``GET passport.bilibili.com/x/passport-login/web/qrcode/generate``
   → 拿到 ``url``（要编码成二维码的内容）和 ``qrcode_key``（轮询凭据）
2. 把 ``url`` 渲染成二维码图片发到群里
3. 每隔几秒 ``GET .../qrcode/poll?qrcode_key=xxx`` 轮询：
   - ``86101`` 未扫描
   - ``86090`` 已扫描，等用户在手机上确认
   - ``86038`` 二维码已过期
   - ``0``     登录成功
4. 成功后，``SESSDATA`` / ``bili_jct`` / ``DedeUserID`` 是通过 poll 接口的
   **``Set-Cookie`` 响应头**下发的（老版会放在 ``data.url`` 的 query 里，
   现在没了）。所以 poll 请求必须用能拿到响应头的封装（``fetch_json_with_cookies``），
   再从 Set-Cookie 里抠出来写进配置。

关于二维码图片：容器里已经有 ``qrcode`` 和 ``PIL``，本地生成 PNG 就行，
不用依赖任何外部二维码服务。

轮询跑在后台任务里（``Context.register_task``），不然会把 handler 挂住几分钟。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

from .http import HttpError, fetch_json, fetch_json_with_cookies, fetch_with_cookies

_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={}"
_NAV = "https://api.bilibili.com/x/web-interface/nav"

# 轮询状态码
CODE_SUCCESS = 0
CODE_EXPIRED = 86038
CODE_WAITING_SCAN = 86101
CODE_WAITING_CONFIRM = 86090

_STATUS_TEXT = {
    CODE_WAITING_SCAN: "等待扫码",
    CODE_WAITING_CONFIRM: "已扫码，请在手机上确认登录",
    CODE_EXPIRED: "二维码已过期",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


class QRCodeUnavailable(RuntimeError):
    """二维码生成失败（缺库之类）。"""


def _qr_image(payload: str) -> Path:
    """把一段文本渲染成二维码 PNG，返回文件路径。"""
    try:
        import qrcode  # 延迟导入：容器里没装的时候给个明确的错误
    except ImportError as exc:  # pragma: no cover
        raise QRCodeUnavailable(
            "未安装 qrcode 库，无法生成二维码。可在容器内执行 "
            "`pip install qrcode[pil]`"
        ) from exc

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    out_dir = Path(tempfile.gettempdir()) / "astrbot_plugin_rconsole" / "qrcode"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "bili_login.png"
    img.save(str(path))
    return path


async def create_login_qrcode() -> tuple[str, str, Path]:
    """申请一个登录二维码。

    Returns:
        ``(qrcode_key, 二维码里的 url, 本地图片路径)``

    Raises:
        HttpError: 接口失败
        QRCodeUnavailable: 本地生成图片失败
    """
    data = await fetch_json(_QR_GENERATE, headers=_HEADERS, retries=1)
    if data.get("code") != 0:
        raise HttpError(f"申请二维码失败: {data.get('message') or data.get('code')}")

    payload = data.get("data") or {}
    url = payload.get("url") or ""
    key = payload.get("qrcode_key") or ""
    if not url or not key:
        raise HttpError("接口没有返回 url / qrcode_key")

    return key, url, _qr_image(url)


async def poll_once(qrcode_key: str) -> tuple[int, dict, dict]:
    """轮询一次。

    Returns:
        ``(状态码, data, cookies)`` —— 拿到 ``CODE_SUCCESS`` 时 cookies 里
        含 B 站通过 Set-Cookie 下发的 SESSDATA / bili_jct / DedeUserID 等。
    """
    data, cookies = await fetch_json_with_cookies(
        _QR_POLL.format(qrcode_key), headers=_HEADERS, retries=1
    )
    payload = data.get("data") or {}
    # 外层 code 是接口本身是否正常，真正表示扫码状态的是 data.code
    code = payload.get("code", data.get("code", -1))
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -1
    return code, payload, cookies


async def wait_for_login(
    qrcode_key: str,
    *,
    timeout: float = 180.0,
    interval: float = 2.0,
) -> dict:
    """轮询到登录成功 / 过期 / 超时。

    Returns:
        成功时返回凭据 dict：``{SESSDATA, bili_jct, DedeUserID, ...}``
        失败时返回 ``{"error": "..."}``
    """
    elapsed = 0.0
    last_status = None

    while elapsed < timeout:
        try:
            code, payload, cookies = await poll_once(qrcode_key)
        except HttpError as exc:
            logger.debug(f"[R插件][B站扫码] 轮询失败（继续）: {exc}")
            await asyncio.sleep(interval)
            elapsed += interval
            continue

        if code != last_status:
            logger.info(
                f"[R插件][B站扫码] 状态 {code} "
                f"{_STATUS_TEXT.get(code, '')}"
            )
            last_status = code

        if code == CODE_SUCCESS:
            # 凭据来源有三层，按可信度从高到低依次覆盖：
            #   1. 老版：data.url 的 query 里直接带 SESSDATA（parse_qs 会 decode）
            #   2. 新版：poll 响应头的 Set-Cookie（保留 %2C 编码，与浏览器一致）
            #   3. 兜底：请求跳转地址，从重定向链的 Set-Cookie 拿
            credentials = parse_credentials(payload.get("url") or "")
            credentials.update(_pick_wanted(cookies))
            if not credentials.get("SESSDATA") and payload.get("url"):
                credentials.update(await _credentials_via_redirect(payload["url"]))

            if not credentials.get("SESSDATA"):
                logger.warning(
                    f"[R插件][B站扫码] 登录成功但拿不到 SESSDATA。"
                    f"data.url={str(payload.get('url'))[:200]!r} "
                    f"cookies={list(cookies.keys())}"
                )
                return {"error": "登录成功但没能从响应里解析到 SESSDATA"}
            credentials["refresh_token"] = payload.get("refresh_token", "")
            credentials["timestamp"] = payload.get("timestamp", 0)
            return credentials

        if code == CODE_EXPIRED:
            return {"error": "二维码已过期，请重新发起"}

        await asyncio.sleep(interval)
        elapsed += interval

    return {"error": f"等待扫码超时（{int(timeout)}秒）"}


def parse_credentials(redirect_url: str) -> dict[str, str]:
    """从登录成功返回的跳转地址里抠出 Cookie 字段。

    形如::

        https://passport.biligame.com/crossDomain?DedeUserID=123&DedeUserID__ckMd5=x
        &Expires=1700000000&SESSDATA=abc%2Cdef&bili_jct=xyz&gourl=https%3A%2F%2F...

    只有 SESSDATA / bili_jct / DedeUserID / buvid3 是我们关心的。
    """
    wanted = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
    result: dict[str, str] = {}
    if not redirect_url:
        return result

    query = urlparse(redirect_url).query
    if not query and "?" in redirect_url:
        query = redirect_url.split("?", 1)[1]

    parsed = parse_qs(query, keep_blank_values=False)
    for key in wanted:
        values = parsed.get(key)
        if values and values[0]:
            result[key] = values[0]
    return result


_WANTED_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")


def _pick_wanted(cookies: dict[str, str]) -> dict[str, str]:
    """从 cookie 字典里挑出我们关心的登录态字段。"""
    result: dict[str, str] = {}
    for key in _WANTED_COOKIES:
        value = cookies.get(key)
        if value:
            result[key] = value
    return result


async def _credentials_via_redirect(url: str) -> dict[str, str]:
    """兜底：请求登录成功返回的跳转地址，从重定向链的 Set-Cookie 抠凭据。"""
    try:
        _, _, cookies = await fetch_with_cookies(url, headers=_HEADERS, retries=1)
    except HttpError as exc:
        logger.warning(f"[R插件][B站扫码] 请求跳转地址拿 Cookie 失败: {exc}")
        return {}
    return _pick_wanted(cookies)


async def fetch_login_state(cookie: str) -> dict:
    """查当前 Cookie 对应的账号信息（原插件的 ``#RBS`` 状态查询）。

    Returns:
        ``{"logged_in": bool, "uname": str, "mid": int, "vip": bool, "msg": str}``
    """
    if not cookie:
        return {"logged_in": False, "msg": "未配置 Cookie"}

    headers = dict(_HEADERS)
    headers["Cookie"] = cookie

    try:
        data = await fetch_json(_NAV, headers=headers, retries=1)
    except HttpError as exc:
        return {"logged_in": False, "msg": f"接口请求失败: {exc}"}

    if data.get("code") != 0:
        return {
            "logged_in": False,
            "msg": data.get("message") or f"接口返回 code={data.get('code')}",
        }

    payload = data.get("data") or {}
    if not payload.get("isLogin"):
        return {"logged_in": False, "msg": "Cookie 已失效或未登录"}

    vip = payload.get("vipStatus") == 1
    return {
        "logged_in": True,
        "uname": payload.get("uname") or "",
        "mid": payload.get("mid") or 0,
        "vip": vip,
        "vip_label": "大会员" if vip else "普通用户",
        "level": payload.get("level_info", {}).get("current_level", 0),
        "money": payload.get("money") or 0,
        "msg": "正常",
    }


def mask_cookie(cookie: str) -> str:
    """把 Cookie 里的敏感值打码，用于日志和回复。"""
    if not cookie:
        return "(空)"

    def _mask(match: re.Match) -> str:
        key, value = match.group(1), match.group(2)
        if len(value) <= 8:
            return f"{key}=***"
        return f"{key}={value[:4]}***{value[-4:]}"

    return re.sub(r"([A-Za-z0-9_\-]+)=([^;]+)", _mask, cookie)
