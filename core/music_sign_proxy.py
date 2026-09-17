"""音乐卡片签名代理。

为什么需要它
============

NapCat / SnowLuma 收到 OneBot 的 ``music`` 段时**不自己生成卡片**，而是把
参数 POST 给一个外部「音卡签名服务」，再把返回的 JSON 当 lightApp 发出去。
QQ 会校验里面的 ``config.token``，不合法就显示「发送者版本过低，无法展示内容」
（这是**通用验证失败提示**，不是字面意义的版本问题）。所以签名服务是必需的。

官方 NapCat 的首选签名服务见其源码 ``packages/napcat-onebot/api/msg.ts``::

    if (!signUrl) signUrl = 'http://106.55.0.102:10087/';  // yibai音卡签名

**本代理负责两件事，都是「在签名之前」把内容摆正**：

一、把「双重编码」的响应展开一层
--------------------------------

上游返回的是 **JSON 字符串字面量**，形如 ``"{...卡片JSON...}"``（外层多一层引号）。

- 官方 NapCat 用 ``RequestUtil.HttpGetJson<string>()``，会先解析一次，所以正常；
- **SnowLuma 直接用 ``resp.text()``** 拿原始响应体，于是 ``JSON.parse`` 得到
  string 而不是 object，触发 ``message element "json" field "text" must contain
  a JSON object`` 校验失败 —— 然后它**降级成字段残缺的本地卡片**，被 QQ 判
  「发送者版本过低」而不显示。

二、按来源平台指定 ``type``，让上游直接签出正确的品牌
----------------------------------------------------

上游卡片里的 ``meta.music.tag`` / ``extra.appid`` 是**由请求里的 ``type`` 决定的**
（实测，两个上游服务行为一致）::

    type=custom  ->  tag="QQ音乐"      appid=100497308（QQ音乐）
    type=163     ->  tag="网易云音乐"  appid=100495085（网易云）

所以点网易云的歌却显示「QQ音乐」，**不是改 response 能修的**——那样会改到
已签名内容，详见下节。正确做法是在**请求**里把 ``type`` 设成 ``163``。

⚠️ **绝不能在签名后改写卡片内容**
----------------------------------

``config.token`` 是**卡片内容的摘要**（实测：同一内容连发 5 次 token 完全相同，
连跨秒都一致；只把 title 改一个字，token 立刻不同且两组无交集）。所以**任何
签名后的字段改写都会让 token 与内容失配，QQ 直接不渲染卡片**。

早期版本用「按 jumpUrl 域名改写 ``meta.music.tag``」的做法纠正平台，正是因此
导致网易云卡片发出去看不见——现已改为上面「请求侧指定 type」的方案。

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
# 官方 NapCat 的备选（ss.xingzhige.com；id 模式已被其关闭，但 custom / 163 仍可用）
FALLBACK_UPSTREAM = "https://ss.xingzhige.com/music_card/card"

DEFAULT_PORT = 18888

# 按歌曲页域名判断来源平台 -> 发给签名服务的 ``type``。
#
# ``custom`` 表示「不带平台品牌」（上游会按 QQ音乐 处理）；
# 其余取值会让上游按该平台的品牌签名（tag / appid / tagIcon 一并换掉）。
#
# 注意：这些都是**请求侧**参数，签名发生在其后，所以 token 天然匹配。
PLATFORM_TYPES: tuple[tuple[str, str], ...] = (
    ("music.163.com", "163"),
    ("y.qq.com", "custom"),
    ("kugou.com", "kugou"),
    ("kuwo.cn", "kuwo"),
    ("migu.cn", "migu"),
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


def _pick_type(url: str) -> str | None:
    """按歌曲页域名挑签名服务要用的 ``type``。"""
    low = (url or "").lower()
    for domain, type_name in PLATFORM_TYPES:
        if domain in low:
            return type_name
    return None


def normalize_sign_request(raw: bytes) -> bytes:
    """把 SnowLuma 发来的签名请求摆正（**签名之前**）。

    做两件事：

    1. **按 ``url`` 域名补/改 ``type``** —— 这是平台品牌（tag/appid）的来源。
       点歌场景下 AstrBot 侧统一发 ``type=custom``（因为它要过 AstrBot 自己的
       Music 组件校验），网易云的品牌就靠这里改成 ``163``。
    2. **把 ``content`` 映射成 ``singer``** —— OneBot 的 music 段用的字段名是
       ``content``，而上游签名服务认的是 ``singer``（官方 NapCat 也是这么转的，
       见 ``api/msg.ts`` 里 ``postData = { singer: content, ...others }``）。
       不补的话卡片的副标题会是空的。

    ``id`` 模式（``{type:"163", id:...}``）现在两个上游都已停用
    （一个返回「无法准确获取歌曲信息」、一个返回 400「缺少 title」），
    所以带 ``id`` 的请求原样转发，交给上游报错。

    解析失败时原样返回，绝不阻断转发。
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return raw
    if not isinstance(payload, dict):
        return raw

    changed = False

    # 1) content -> singer（上游只认 singer）
    if payload.get("content") and not payload.get("singer"):
        payload["singer"] = payload["content"]
        changed = True

    # 2) 按域名指定平台 type（不带 id 的 custom 模式才有意义）
    if payload.get("id") is None:
        want = _pick_type(str(payload.get("url") or ""))
        if want and payload.get("type") != want:
            payload["type"] = want
            changed = True

    if not changed:
        return raw
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return raw


class SignProxy:
    """转发签名请求（顺带摆正请求体、展开响应）的本地小服务。"""

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

        上游是公益服务，**偶发 504「等待 Response 超时」**（实测遇到过，
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

        body = normalize_sign_request(raw)
        try:
            preview = body[:200].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            preview = "<binary>"

        try:
            status, text = await self._forward(body)
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[R插件] 音乐签名上游请求失败: {self.last_error}")
            err = json.dumps({"error": self.last_error}).encode()
            return web.Response(status=502, body=err, content_type="application/json")

        if status != 200:
            self.stats["failed"] += 1
            self.last_error = f"上游 HTTP {status}: {text[:200]}"
            logger.warning(f"[R插件] 音乐签名上游异常: {self.last_error}")
            return web.Response(
                status=status,
                text=text,
                content_type="application/json",
            )

        # 只做「展开一层」，**不改内容** —— token 是内容摘要，改了就失效
        card = unwrap_sign_response(text)
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
