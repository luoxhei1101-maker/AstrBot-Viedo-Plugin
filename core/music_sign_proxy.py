"""音乐卡片签名代理。

为什么需要它
============

NapCat / SnowLuma 收到 OneBot 的 ``music`` 段时**不自己生成卡片**，而是把
参数 POST 给一个外部「音卡签名服务」，再把返回的 JSON 当 lightApp 发出去。
QQ 会校验里面的 ``config.token``，不合法就显示「发送者版本过低，无法展示内容」
（这是**通用验证失败提示**，不是字面意义的版本问题）。所以签名服务是必需的。

官方 NapCat 的首选签名服务见其源码 ``packages/napcat-onebot/api/msg.ts``::

    if (!signUrl) signUrl = 'http://106.55.0.102:10087/';  // yibai音卡签名

但直接指向它在本项目环境下有两个问题，都需要在本代理里修掉：

**问题一：响应是「双重编码」的**

上游返回的是 **JSON 字符串字面量**，形如 ``"{...卡片JSON...}"``（外层多一层引号）。

- 官方 NapCat 用 ``RequestUtil.HttpGetJson<string>()``，会先解析一次拿到字符串，
  再交给转换器，所以正常；
- **SnowLuma 直接用 ``resp.text()``** 拿原始响应体，于是 ``JSON.parse`` 得到的是
  string 而不是 object，触发 ``message element "json" field "text" must contain
  a JSON object`` 校验失败 —— 然后它**降级成字段残缺的本地卡片**，被 QQ 判
  「发送者版本过低」而不显示。

本代理把响应**展开一层**，交回裸 JSON 对象，SnowLuma 的校验就通过了。

**问题二：平台标识是写死的**

上游返回的卡片里 ``meta.music.tag`` 恒为 ``"QQ音乐"``。于是点网易云的歌时，
卡片会显示「QQ音乐」标签（但点击跳转是对的，显得很矛盾）。
这里按 ``meta.music.jumpUrl`` 的域名判断真实来源平台并改写 ``tag`` / ``tagIcon``。

工作方式
========

插件启动时在本地起一个极小的 HTTP 服务（默认 18888），把 SnowLuma 的
``config/onebot_<uin>.json`` 里的 ``musicSignUrl`` 指向它即可::

    {"musicSignUrl": "http://astrbot:18888/"}

上游地址、监听端口都可在插件配置里改。
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - AstrBot 自带 aiohttp，正常不会走到
    web = None  # type: ignore[assignment]


# 官方 NapCat 首选签名服务（源码里写死的默认值）
DEFAULT_UPSTREAM = "http://106.55.0.102:10087/"
# 官方 NapCat 的备选（ss.xingzhige.com，id 模式已被其关闭，但 custom 仍可用）
FALLBACK_UPSTREAM = "https://ss.xingzhige.com/music_card/card"

DEFAULT_PORT = 18888

# 按 jumpUrl 域名判断来源平台 -> (标签文字, 标签图标)
#
# 图标留空时 QQ 会退回默认样式，比挂一个错平台的图标要好。
PLATFORM_TAGS: tuple[tuple[str, str, str], ...] = (
    ("music.163.com", "网易云音乐", "https://s1.music.126.net/style/favicon.ico"),
    ("y.qq.com", "QQ音乐", "https://p.qpic.cn/qqconnect/0/app_100497308_1626060999/100"),
    ("kugou.com", "酷狗音乐", ""),
    ("kuwo.cn", "酷我音乐", ""),
    ("bilibili.com", "哔哩哔哩", ""),
)


def unwrap_sign_response(raw: str) -> str:
    """把上游的「双重编码」响应展开一层。

    上游返回 ``'"{...}"'``（JSON 字符串字面量）时取出里面的字符串；
    已是裸 JSON 对象则原样返回。解析失败也原样返回，交给上层报错。
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(value, str):
        return value
    return raw


def _pick_platform(jump_url: str) -> tuple[str, str] | None:
    low = (jump_url or "").lower()
    for domain, tag, icon in PLATFORM_TAGS:
        if domain in low:
            return tag, icon
    return None


def fix_platform_tag(card_json: str) -> str:
    """按 jumpUrl 改写卡片里的平台标识（tag / tagIcon）。

    签名服务固定写 ``tag: "QQ音乐"``，点网易云的歌也会显示成 QQ音乐，
    这里按域名纠正。解析失败或结构不符则原样返回，不影响主流程。
    """
    try:
        card = json.loads(card_json)
    except (ValueError, TypeError):
        return card_json
    if not isinstance(card, dict):
        return card_json

    meta = card.get("meta")
    music = meta.get("music") if isinstance(meta, dict) else None
    if not isinstance(music, dict):
        return card_json

    picked = _pick_platform(str(music.get("jumpUrl") or ""))
    if not picked:
        return card_json

    tag, icon = picked
    if music.get("tag") == tag:
        return card_json
    music["tag"] = tag
    if icon:
        music["tagIcon"] = icon
    else:
        music.pop("tagIcon", None)
    try:
        return json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return card_json


class SignProxy:
    """转发签名请求、修正响应的本地小服务。"""

    def __init__(self, upstream: str = "", port: int = DEFAULT_PORT) -> None:
        self.upstream = (upstream or DEFAULT_UPSTREAM).strip() or DEFAULT_UPSTREAM
        self.port = int(port or DEFAULT_PORT)
        self._runner: Any = None
        self._site: Any = None
        self._session: Any = None
        self.stats = {"requests": 0, "ok": 0, "failed": 0}
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._runner is not None

    def url(self) -> str:
        """给 SnowLuma 填的建议地址（用容器名，跨容器可达）。"""
        return f"http://astrbot:{self.port}/"

    def _upstreams(self) -> list[str]:
        """上游地址列表：先配的，再内置默认，最后官方备选（去重）。"""
        out: list[str] = []
        for url in (self.upstream, DEFAULT_UPSTREAM, FALLBACK_UPSTREAM):
            url = (url or "").strip()
            if url and url not in out:
                out.append(url)
        return out

    async def _forward(self, body: bytes) -> tuple[int, str]:
        """请求上游签名服务，失败自动换下一个。

        上游是公益服务，**偶发 504「等待 Response 超时」**（实测遇到过一次，
        直接导致卡片降级成残缺的本地卡片）。所以这里逐个试，只有全部失败
        才把最后的错误交回去。

        返回 ``(状态码, 响应文本)``。
        """
        from .http import get_session

        if self._session is None:
            self._session = get_session()
        headers = {"Content-Type": "application/json", "User-Agent": "NapCat"}

        last: tuple[int, str] = (502, '{"error":"no upstream configured"}')
        for url in self._upstreams():
            try:
                async with self._session.post(
                    url, data=body, headers=headers, timeout=25
                ) as resp:
                    text = await resp.text()
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning(f"[R插件] 音乐签名上游不可用 {url}: {detail}")
                last = (502, json.dumps({"error": detail}))
                continue

            if resp.status == 200:
                if url != self.upstream:
                    logger.info(f"[R插件] 音乐签名改用备选上游: {url}")
                return resp.status, text

            logger.warning(
                f"[R插件] 音乐签名上游返回 HTTP {resp.status}（{url}）: {text[:120]}"
            )
            last = (resp.status, text)

        self.last_error = f"全部上游失败，最后一次: HTTP {last[0]}"
        return last


    async def _handle(self, request: Any) -> Any:
        """处理 SnowLuma 发来的签名请求。"""
        raw = await request.read()
        self.stats["requests"] += 1
        try:
            preview = raw[:200].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            preview = "<binary>"

        try:
            status, text = await self._forward(raw)
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[R插件] 音乐签名上游请求失败: {self.last_error}")
            body = json.dumps({"error": self.last_error}).encode()
            return web.Response(status=502, body=body, content_type="application/json")

        if status != 200:
            self.stats["failed"] += 1
            self.last_error = f"上游 HTTP {status}: {text[:200]}"
            logger.warning(f"[R插件] 音乐签名上游异常: {self.last_error}")
            return web.Response(
                status=status,
                text=text,
                content_type="application/json",
            )

        card = fix_platform_tag(unwrap_sign_response(text))
        self.stats["ok"] += 1
        logger.debug(f"[R插件] 音乐签名转发成功 {len(card)}B  请求={preview[:80]}")
        return web.Response(text=card, content_type="application/json")

    async def start(self) -> bool:
        """启动代理。成功返回 True。"""
        if web is None:
            logger.warning("[R插件] 音乐签名代理未启动：aiohttp 不可用")
            return False
        if self._runner is not None:
            return True
        try:
            app = web.Application()
            app.router.add_post("/", self._handle)
            app.router.add_get("/health", self._health)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", self.port)
            await site.start()
        except OSError as exc:
            logger.warning(
                f"[R插件] 音乐签名代理端口 {self.port} 不可用（{exc}），"
                "音乐卡片可能无法发送"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件] 音乐签名代理启动失败: {type(exc).__name__}: {exc}")
            return False

        self._runner, self._site = runner, site
        logger.info(
            f"[R插件] 音乐签名代理已启动: 0.0.0.0:{self.port} -> {self.upstream}"
        )
        logger.info(
            f"[R插件] 请把 SnowLuma 的 musicSignUrl 设为 {self.url()}"
            "（改完需重启 snowluma 容器）"
        )
        return True

    async def _health(self, request: Any) -> Any:  # noqa: ARG002
        return web.json_response(
            {
                "running": self.running,
                "port": self.port,
                "upstream": self.upstream,
                "stats": self.stats,
                "last_error": self.last_error,
            }
        )

    async def stop(self) -> None:
        """优雅关闭。"""
        if self._runner is None:
            return
        try:
            await self._runner.cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件] 音乐签名代理关闭异常: {exc}")
        finally:
            self._runner = None
            self._site = None
            self._session = None
            logger.info("[R插件] 音乐签名代理已关闭")
