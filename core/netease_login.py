"""网易云扫码登录。

**不需要自建 NeteaseCloudMusicApi** —— 官方网页端就有扫码接口，实测可用：

1. ``GET https://music.163.com/api/login/qrcode/unikey?type=1``
   -> ``{"code":200,"unikey":"35284f9a-..."}``
2. 把 ``https://music.163.com/login?codekey=<unikey>`` 画成二维码给用户扫
3. 轮询 ``GET .../api/login/qrcode/client/login?key=<unikey>&type=1``

轮询的 ``code`` 含义：

======  ==========================
code    含义
======  ==========================
800     二维码过期，需要重新获取
801     等待扫码
802     已扫码，等用户在手机上确认
803     授权成功，响应体里带 cookie
======  ==========================

803 时凭据既可能在响应体的 ``cookie`` 字段里，也可能在 ``Set-Cookie`` 头里，
两种都兜住（实测以 ``cookie`` 字段为主）。

登录成功后只需要 ``MUSIC_U`` 就能取直链，但整串一起存更好 —— ``__csrf`` 之类
在后面做需要 CSRF 的接口（如收藏、云盘）时会用到。
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from astrbot.api import logger

# 二维码内容的前缀（官方网页端扫码用的就是这个）
QR_CONTENT = "https://music.163.com/login?codekey={key}"

UNIKEY_API = "https://music.163.com/api/login/qrcode/unikey?type=1"
POLL_API = "https://music.163.com/api/login/qrcode/client/login?key={key}&type=1"
ACCOUNT_API = "https://music.163.com/api/nuser/account/get"
VIP_API = "https://music.163.com/api/music-vip-membership/front/vip/info"

# 扫码时带上网页端那套「客户端伪装」，不带会拿不到 unikey
_LOGIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Cookie": "os=pc; appver=2.9.7;",
}

# 轮询状态码 -> 提示语
CODE_TEXT = {
    800: "二维码已过期，请重新获取",
    801: "等待扫码中…",
    802: "已扫码，请在手机上点「确认登录」",
    803: "登录成功",
}


async def _request(url: str, cookie: str = "", timeout: float = 15.0):
    """发一个 GET，返回 ``(状态码, 文本, Set-Cookie 列表)``。"""
    from .http import get_session

    headers = dict(_LOGIN_HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    session = get_session()
    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            text = await resp.text()
            # aiohttp 的 Set-Cookie 要用 headers.getall
            try:
                cookies = resp.headers.getall("Set-Cookie", [])
            except (AttributeError, TypeError):
                cookies = [resp.headers.get("Set-Cookie", "")]
            return resp.status, text, [c for c in cookies if c]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", []


def _parse_json(text: str) -> dict:
    import json

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def make_qr_png(content: str) -> bytes | None:
    """把内容画成二维码 PNG。缺 qrcode 库时返回 None。"""
    try:
        import qrcode

        img = qrcode.make(content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][网易云扫码] 生成二维码失败: {type(exc).__name__}: {exc}")
        return None


async def create_login() -> tuple[str, bytes | None]:
    """申请一个 unikey 并生成二维码。

    Returns:
        ``(unikey, png_bytes)``；申请失败时 unikey 为空串。
    """
    status, text, _ = await _request(UNIKEY_API)
    data = _parse_json(text)
    unikey = str(data.get("unikey") or "")
    if status != 200 or not unikey:
        logger.warning(f"[R插件][网易云扫码] 取 unikey 失败 HTTP {status}: {text[:160]}")
        return "", None
    png = await asyncio.to_thread(make_qr_png, QR_CONTENT.format(key=unikey))
    return unikey, png


def extract_cookie(body_cookie: str, set_cookies: list[str]) -> str:
    """从「响应体 cookie 字段」或「Set-Cookie 头」里凑出可用的 Cookie 串。

    优先用响应体的 ``cookie`` 字段（官方给的是完整串）；没有就退回拼
    Set-Cookie，只保留 ``MUSIC_U`` / ``__csrf`` / ``NMTID`` 这几个有用的。
    """
    if body_cookie and "MUSIC_U" in body_cookie:
        return body_cookie.strip().rstrip(";").strip()

    wanted: dict[str, str] = {}
    for raw in set_cookies:
        head = raw.split(";", 1)[0].strip()
        if "=" not in head:
            continue
        key, _, value = head.partition("=")
        key = key.strip()
        if key in ("MUSIC_U", "__csrf", "NMTID") and value:
            wanted[key] = value.strip()
    if not wanted:
        return ""
    return "; ".join(f"{k}={v}" for k, v in wanted.items())


async def poll(unikey: str) -> dict:
    """轮询一次扫码状态。

    Returns:
        ``{"code": int, "message": str, "cookie": str}``；code 为 0 表示请求本身失败。
    """
    status, text, set_cookies = await _request(POLL_API.format(key=unikey))
    data = _parse_json(text)
    if status != 200 or not data:
        return {"code": 0, "message": f"HTTP {status}: {text[:120]}", "cookie": ""}

    code = int(data.get("code") or 0)
    cookie = ""
    if code == 803:
        cookie = extract_cookie(
            str(data.get("cookie") or ""),
            [c for c in set_cookies if "MUSIC_U" in c] or set_cookies,
        )
    return {
        "code": code,
        "message": str(data.get("message") or CODE_TEXT.get(code, "")),
        "cookie": cookie,
    }


async def wait_for_login(
    unikey: str, timeout: float = 180.0, interval: float = 3.0
) -> dict:
    """轮询直到成功 / 过期 / 超时。

    Returns:
        成功：``{"cookie": "...", "message": "登录成功"}``
        失败：``{"error": "原因"}``
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(10.0, timeout)
    last = 801

    while loop.time() < deadline:
        # 取消要能立刻生效，所以 sleep 放在每次轮询之后
        await asyncio.sleep(interval)
        result = await poll(unikey)
        code = result["code"]
        last = code

        if code == 803:
            if not result["cookie"]:
                logger.warning("[R插件][网易云扫码] 803 但没解析出 Cookie")
                return {"error": "扫码成功但没拿到 Cookie，请重试"}
            return {"cookie": result["cookie"], "message": result["message"]}
        if code == 800:
            return {"error": "二维码已过期，请重新发送命令获取"}

    logger.info(f"[R插件][网易云扫码] 轮询超时（最后一次 code={last}）")
    return {"error": "扫码超时，请重新发送命令获取二维码"}


async def fetch_account(cookie: str) -> dict:
    """用 Cookie 查账号信息（昵称 / 会员状态）。

    两个接口都验过：

    - ``nuser/account/get`` -> ``profile.nickname`` + ``account.vipType``
    - ``music-vip-membership/front/vip/info`` -> ``data.redVipLevel`` 等

    Returns:
        ``{"ok": bool, "nickname": str, "user_id": int, "vip_label": str, ...}``
    """
    if not cookie:
        return {"ok": False, "msg": "未配置"}

    status, text, _ = await _request(ACCOUNT_API, cookie)
    data = _parse_json(text)
    profile = data.get("profile") or {}
    account = data.get("account") or {}
    if status != 200 or not profile.get("nickname"):
        return {"ok": False, "msg": "未登录或 Cookie 已失效"}

    out: dict[str, Any] = {
        "ok": True,
        "nickname": str(profile.get("nickname") or ""),
        "user_id": profile.get("userId") or account.get("id") or 0,
        "avatar": str(profile.get("avatarUrl") or ""),
        "vip_type": account.get("vipType"),
        "vip_label": "",
        "vip_level": 0,
        "vip_expire": "",
    }

    # 会员等级（拿不到不影响主流程）
    status2, text2, _ = await _request(VIP_API, cookie)
    vip = _parse_json(text2).get("data") or {}
    if status2 == 200 and vip:
        level = vip.get("redVipLevel") or (vip.get("musicPackage") or {}).get("vipLevel")
        if level:
            out["vip_level"] = int(level)
            out["vip_label"] = f"黑胶VIP Lv.{level}"
        expire = (vip.get("musicPackage") or {}).get("expireTime") or 0
        if expire:
            import datetime

            try:
                ts = int(expire) / 1000
                out["vip_expire"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass

    if not out["vip_label"]:
        # 兜底：vipType 非 0 就算会员（11 = 黑胶VIP）
        vt = out["vip_type"]
        if vt:
            out["vip_label"] = "黑胶VIP" if int(vt) >= 10 else "音乐包"
        else:
            out["vip_label"] = "无会员"
    return out
