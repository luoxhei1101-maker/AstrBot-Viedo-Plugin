"""Cookie 状态检查：并行查各平台 Cookie 是否仍有效，并取账号昵称 / 会员等级。

展示原则（用户要求）：**只给「平台 + 昵称 + 状态」，绝不回显 Cookie 本身**。
音乐平台额外给会员状态和等级。

各平台的校验接口（都是实测过的）：

============  ==========================================================
平台           接口
============  ==========================================================
B站            ``api.bilibili.com/x/web-interface/nav``
              -> ``uname`` / ``vipStatus`` / ``level_info.current_level``
网易云         ``music.163.com/api/nuser/account/get``
              -> ``profile.nickname`` / ``account.vipType``
              + ``music-vip-membership/front/vip/info`` -> ``redVipLevel``
QQ音乐         ``u.y.qq.com/cgi-bin/musicu.fcg``::

                module=music.UserInfo.userInfoServer
                method=GetLoginUserInfo   -> info.nick
                module=VipLogin.VipLoginInter
                method=vip_login_base     -> identity.level / svip / eightEnd

============  ==========================================================

⚠️ QQ音乐这两个接口都**必须带完整的 ``comm`` 块**（uin / authst /
tmeLoginType / guid …）—— 只塞裸 comm 会稳定返回 ``code=1000``（未登录），
这是踩过的坑。

抖音 / 快手 / 小红书 / 视频号没有可用的公开校验接口，标「已配置」而不是
谎报「有效」。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from astrbot.api import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

STATUS_OK = "ok"
STATUS_BAD = "bad"
STATUS_OFF = "off"
STATUS_WARN = "warn"


async def _get_json(url: str, cookie: str = "", headers: dict | None = None,
                    timeout: float = 15.0) -> tuple[int, Any]:
    from .http import get_session

    h = {"User-Agent": UA}
    if cookie:
        h["Cookie"] = cookie
    if headers:
        h.update(headers)
    try:
        session = get_session()
        async with session.get(url, headers=h, timeout=timeout) as resp:
            text = await resp.text()
            status = resp.status
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][Cookie状态] {url[:60]} 请求失败: {exc}")
        return 0, None
    try:
        return status, json.loads(text)
    except (ValueError, TypeError):
        return status, None


async def _post_json(url: str, payload: dict, cookie: str = "",
                     timeout: float = 15.0) -> tuple[int, Any]:
    from .http import get_session

    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    try:
        session = get_session()
        async with session.post(
            url, data=json.dumps(payload, ensure_ascii=False).encode(),
            headers=h, timeout=timeout,
        ) as resp:
            text = await resp.text()
            status = resp.status
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][Cookie状态] {url[:60]} 请求失败: {exc}")
        return 0, None
    try:
        return status, json.loads(text)
    except (ValueError, TypeError):
        return status, None


def _row(platform: str, label: str, status: str, nickname: str = "",
         detail: str = "") -> dict:
    return {"platform": platform, "label": label, "status": status,
            "nickname": nickname, "detail": detail}


# ----------------------------------------------------------------------
# 各平台
# ----------------------------------------------------------------------


async def _check_bili(label: str, cookie: str) -> dict:
    status, data = await _get_json(
        "https://api.bilibili.com/x/web-interface/nav", cookie,
        {"Referer": "https://www.bilibili.com/"},
    )
    if status != 200 or not isinstance(data, dict):
        return _row("bili", label, STATUS_WARN, "", "接口不可达")
    d = data.get("data") or {}
    if not d.get("isLogin"):
        return _row("bili", label, STATUS_BAD, "", "未登录 / Cookie 已失效")
    vip = "大会员" if d.get("vipStatus") else "非大会员"
    lv = (d.get("level_info") or {}).get("current_level")
    return _row("bili", label, STATUS_OK, str(d.get("uname") or ""),
                f"{vip}　Lv{lv}" if lv is not None else vip)


async def _check_netease(label: str, cookie: str) -> dict:
    from .netease_login import fetch_account

    info = await fetch_account(cookie)
    if not info.get("ok"):
        return _row("netease", label, STATUS_BAD, "", info.get("msg") or "已失效")
    detail = info.get("vip_label") or "无会员"
    if info.get("vip_expire"):
        detail += f"　到期 {info['vip_expire']}"
    return _row("netease", label, STATUS_OK, str(info.get("nickname") or ""), detail)


def _qq_comm(ck: dict) -> dict:
    """QQ 音乐的登录 comm 块（缺字段会稳定返回 1000）。"""
    uin = ck.get("uin") or ck.get("wxuin") or "0"
    return {
        "uin": str(uin),
        "format": "json",
        "ct": 24,
        "cv": 4747474,
        "authst": ck.get("qqmusic_key") or ck.get("qm_keyst") or "",
        "tmeLoginType": 1 if ck.get("wxunionid") else 2,
        "guid": hashlib.md5((str(uin) + "music").encode()).hexdigest(),
        "psrf_qqopenid": ck.get("psrf_qqopenid", ""),
        "psrf_qqunionid": ck.get("psrf_qqunionid", ""),
        "psrf_qqaccess_token": ck.get("psrf_qqaccess_token", ""),
        "psrf_qqrefresh_token": ck.get("psrf_qqrefresh_token", ""),
        "qq": str(uin),
        "miniversion": 0,
        "platform": "h5",
        "aid": 55911122,
        "os_ver": "10",
        "network_type": 1,
    }


async def _qq_call(cookie: str, module: str, method: str,
                   param: dict | None = None) -> dict:
    from .music_search import parse_cookie

    ck = parse_cookie(cookie)
    if not (ck.get("qqmusic_key") or ck.get("qm_keyst")):
        return {}
    payload = {
        "comm": _qq_comm(ck),
        "req_0": {"module": module, "method": method, "param": param or {}},
    }
    status, data = await _post_json(
        "https://u.y.qq.com/cgi-bin/musicu.fcg", payload, cookie
    )
    if status != 200 or not isinstance(data, dict):
        return {}
    return data.get("req_0") or {}


async def _check_qqmusic(label: str, cookie: str) -> dict:
    # 昵称与 VIP 是两个独立接口，并行发
    nick_req, vip_req = await asyncio.gather(
        _qq_call(cookie, "music.UserInfo.userInfoServer", "GetLoginUserInfo"),
        _qq_call(cookie, "VipLogin.VipLoginInter", "vip_login_base"),
    )
    info = ((nick_req.get("data") or {}).get("info") or {}) if nick_req else {}
    nick = str(info.get("nick") or "")
    if not nick:
        return _row("qqmusic", label, STATUS_BAD, "",
                    "未登录 / Cookie 已失效（qqmusic_key 约 12 小时过期）")

    detail = "无会员"
    vip_data = (vip_req.get("data") or {}) if vip_req else {}
    ident = vip_data.get("identity") or {}
    if ident:
        level = ident.get("level")
        if vip_data.get("svip"):
            detail = f"豪华绿钻　Lv{level}"
        elif ident.get("vip"):
            detail = f"绿钻　Lv{level}"
        elif level:
            detail = f"非会员　Lv{level}"
    elif vip_data.get("eight"):
        # 没有绿钻但有 8 元音乐包
        end = ident.get("eightEnd") or vip_data.get("send") or ""
        detail = f"音乐包{'　到期 ' + end if end else ''}"
    return _row("qqmusic", label, STATUS_OK, nick, detail)


async def _check_weibo(label: str, cookie: str) -> dict:
    status, data = await _get_json("https://weibo.com/ajax/profile/info", cookie)
    if status != 200 or not isinstance(data, dict):
        return _row("weibo", label, STATUS_WARN, "", "接口不可达")
    user = ((data.get("data") or {}).get("user") or {})
    name = str(user.get("screen_name") or "")
    if not name:
        return _row("weibo", label, STATUS_BAD, "", "未登录 / Cookie 已失效")
    return _row("weibo", label, STATUS_OK, name, "已登录")


async def _check_miyoushe(label: str, cookie: str) -> dict:
    status, data = await _get_json(
        "https://bbs-api.miyoushe.com/user/wapi/getUserFullInfo?gids=2", cookie,
        {"Referer": "https://www.miyoushe.com/"},
    )
    if status != 200 or not isinstance(data, dict):
        return _row("miyoushe", label, STATUS_WARN, "", "接口不可达")
    info = ((data.get("data") or {}).get("user_info") or {})
    name = str(info.get("nickname") or "")
    if not name:
        return _row("miyoushe", label, STATUS_BAD, "", "未登录 / Cookie 已失效")
    return _row("miyoushe", label, STATUS_OK, name, "已登录")


async def _check_xiaoheihe(label: str, cookie: str) -> dict:
    status, data = await _get_json("https://api.xiaoheihe.cn/account/get_user_info/", cookie)
    if status != 200 or not isinstance(data, dict):
        return _row("xiaoheihe", label, STATUS_WARN, "", "接口不可达")
    result = data.get("result") or {}
    name = str(result.get("username") or result.get("nickname") or "")
    if not name:
        return _row("xiaoheihe", label, STATUS_BAD, "", "未登录 / Cookie 已失效")
    return _row("xiaoheihe", label, STATUS_OK, name, "已登录")


# 没有公开校验接口的平台：只报「已配置」，不谎报有效
_CHECKERS: dict[str, Any] = {
    "bili": _check_bili,
    "netease": _check_netease,
    "qqmusic": _check_qqmusic,
    "weibo": _check_weibo,
    "miyoushe": _check_miyoushe,
    "xiaoheihe": _check_xiaoheihe,
}


async def check_one(platform: str, label: str, cookie: str) -> dict:
    """检查单个平台。任何异常都变成一行「无法校验」，不影响其它平台。"""
    if not cookie:
        return _row(platform, label, STATUS_OFF, "", "未配置")
    checker = _CHECKERS.get(platform)
    if checker is None:
        return _row(platform, label, STATUS_WARN, "", "已配置（该平台暂无可用的校验接口）")
    try:
        return await checker(label, cookie)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][Cookie状态] {platform} 校验异常: {exc}")
        return _row(platform, label, STATUS_WARN, "", "校验出错")


async def check_all(items: list[tuple[str, str, str]]) -> tuple[list[dict], str]:
    """并行检查所有平台。

    Args:
        items: ``[(platform_key, label, cookie)]``

    Returns:
        ``(rows, checked_at)``
    """
    started = time.time()
    rows = await asyncio.gather(
        *[check_one(p, label, ck) for p, label, ck in items]
    )
    # 固定顺序：有 Cookie 的排前面，其余按原顺序
    order = {STATUS_OK: 0, STATUS_BAD: 1, STATUS_WARN: 2, STATUS_OFF: 3}
    out = sorted(rows, key=lambda r: order.get(r["status"], 9))
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    logger.info(
        f"[R插件][Cookie状态] 检查 {len(items)} 个平台，"
        f"用时 {time.time() - started:.1f}s"
    )
    return out, stamp
