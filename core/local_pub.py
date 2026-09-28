"""本地图片 → 公网直链（给腾讯服务器自己下载）。

为什么需要它
------------
QQ 官方机器人（``qq_official``）发本地图时，AstrBot 适配器**只有 base64 一条路**：

    # qqofficial_message_event.py
    if image_is_local:
        image_base64 = resolved.to_base64()        # 本地文件 -> base64
    else:
        image_base64 = await resolver.to_base64()  # URL 也先下载再 base64
    ...
    payload = {"file_data": image_base64, ...}     # 根本没有 url 字段

对「境外机器 → 腾讯境内 API」这条链路，把文件整个传上去是最慢的一环。实测：

    97KB  图  base64 上传  7.39s / 14.88s
    784KB 图  url 上传     3.47s          ← 体积大 8 倍，反而快一倍多

腾讯的富媒体接口本身支持 ``{"file_type": 1, "url": "..."}`` —— 平台**自己去下载**，
我们一个字节都不用传。所以只要把本地文件变成一个**公网可访问的 URL** 就行。

URL 从哪来：白嫖官机自己的 WebUI 端口（默认就是这么干）
--------------------------------------------------------
AstrBot 的 dashboard（默认 6185）**本来就映射到公网**（不然用户进不去面板），
而它的认证中间件只拦 ``/api``：

    # astrbot/dashboard/server.py
    async def auth_middleware(self, request):
        path = request.url.path
        if not path.startswith("/api"):     # 非 /api 一律放行
            return None

同时 WebUI 的前端目录是**静态目录**。于是：

    往 <静态根>/rconsole-pub/ 写文件  →  http://<公网IP>:6185/rconsole-pub/<文件>  免认证可访问

实测（2026-09-27，广州 → 服务器）：``HTTP 200  846911 B  image/png``；
不存在的文件返回 404（静态目录优先，不会被 SPA 首页吃掉）；
``/api/...`` 返回 401（认证确实只拦 /api）。

**好处**：用户零配置 —— 不用搭 nginx、不用开端口、不用填地址。

⚠️ 前提：这台机器得让腾讯访问得到
---------------------------------
这条链路是「**腾讯服务器来下载我们的图**」，所以 AstrBot 所在的机器必须
**有公网 IP，而且端口真的能从公网连进来**（云服务器一般满足；家用电脑、
路由器 NAT 后面、纯内网部署都不满足）。

**环境不对时，探测阶段是看不出来的**：静态目录自检走 ``127.0.0.1``（当然通），
公网 IP 探测拿到的是**出口 IP**（也有值）—— 两个都"成功"，拼出来的地址外人
却摸不到。代价是**每次发图都白等一次腾讯的上传超时**（25 秒）。

所以设了两道防线：

1. ``localPubEnabled`` **默认关闭** —— 环境合适才由用户主动打开；
2. 就算误开了也不会一直踩坑 —— :func:`note_transfer_failure` 统计「腾讯取不到图」
   的连续次数，够了就**自动停用** :data:`TRIP_SECONDS` 秒（30 分钟），
   期间照旧走 base64 上传，并写一条日志说明原因。

三种模式（优先级从高到低）
--------------------------
1. **自定义前缀**（``localPubBaseUrl`` 填了）：完全接管，不再拼 ``/rconsole-pub/``。
   适合「自己搭了 HTTP 服务」的场景。
2. **手动 AstrBot 地址**（``localPubPublicUrl`` 填了）：只覆盖 ``scheme://host:port``
   这一段，路径仍由插件拼。**自动探测的地址不通时就填这个**，例如
   ``https://bot.example.com``（nginx 反代到 6185）或宿主映射成别的端口的情况。
3. **自动探测**（都留空）：探静态根 + 端口 + 公网 IP。注意这一步只验证
   「本机 ``127.0.0.1`` 能不能读到静态目录」，**验证不了公网可达性** ——
   对外是否真通，靠 :func:`note_transfer_failure` 的熔断兜底。

⚠️ **任何一步失败都返回 ``None``**，调用方自动退回 base64 —— 绝不会出现
「配了反而发不出图」。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import NamedTuple

from astrbot.api import logger

#: 静态根下面的子目录名。**不要改**（改了旧文件就找不到了）。
SUBDIR = "rconsole-pub"

#: 手动模式下没指定目录时的落盘位置。
LEGACY_DIR = "/AstrBot/data/rconsole_pub"

#: 文件保留时长（秒）。太短可能被腾讯下载前就删了，太长目录会涨。
DEFAULT_TTL = 600

#: 单文件上限（字节）。太大就别走这条路了（也避免撑爆磁盘）。
MAX_BYTES = 32 * 1024 * 1024

#: 只允许这几种后缀，避免被塞进奇怪的扩展名让静态服务执行。
_SAFE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                  ".mp4", ".mp3", ".silk", ".bin"}

#: 探针文件前缀（自检用，写完就删）。
_PROBE_PREFIX = "__rcprobe_"

#: 我们自己生成的文件名长这样：24 位小写 hex + 白名单后缀。
_OUR_NAME_RE = re.compile(r"^[0-9a-f]{24}\.(png|jpg|jpeg|gif|webp|bmp|mp4|mp3|silk|bin)$")

#: 探测公网 IP 的候选（任意一个成功即可）。
_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ipinfo.io/ip",
    "https://api.ip.sb/ip",
)


class PublishTarget(NamedTuple):
    """发布目标：落盘目录 + URL 前缀（不含结尾斜杠）。"""

    directory: Path
    base_url: str

    def url_for(self, name: str) -> str:
        return f"{self.base_url}/{name}"


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------
def normalize_base(url: str) -> str:
    """归一化一个 URL 前缀：去空白、去尾部斜杠。

    ``http://1.2.3.4:6185/`` 和 ``http://1.2.3.4:6185`` 等价；
    用户误填整条直链（``.../rconsole-pub/xxx.png``）时也能救回来。
    """
    return (url or "").strip().rstrip("/")


def strip_asset_path(url: str) -> str:
    """把用户误填的「资源路径」削掉，只留 ``scheme://host[:port]`` 前缀。

    有人会直接把某张图的地址粘过来（``http://ip:6185/rconsole-pub/abc.png``），
    这里削到 ``http://ip:6185``，免得拼出双份路径。
    """
    base = normalize_base(url)
    if not base:
        return ""
    # 有 scheme 就按 scheme 切；没有就原样（允许用户只填 host:port）
    m = re.match(r"^(https?://[^/]+)(.*)$", base, re.I)
    if not m:
        return base.rstrip("/")
    head, tail = m.group(1), m.group(2).rstrip("/")
    # tail 只剩 /SUBDIR 或整条资源路径，都丢掉
    return head


def safe_suffix(suffix: str) -> str:
    """只接受白名单后缀，其余一律当 ``.bin``。"""
    s = (suffix or "").strip().lower()
    if not s:
        return ".bin"
    if not s.startswith("."):
        s = "." + s
    return s if s in _SAFE_SUFFIXES else ".bin"


def content_name(data: bytes, suffix: str = ".png") -> str:
    """按内容哈希生成文件名 —— 同样的图只落一份，天然去重。"""
    return hashlib.sha256(data).hexdigest()[:24] + safe_suffix(suffix)


# ----------------------------------------------------------------------
# 探测：静态目录 / 端口 / 公网 IP
# ----------------------------------------------------------------------
def _data_dir() -> Path:
    try:
        from astrbot.api.star import get_astrbot_data_path

        return Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001
        return Path("/AstrBot/data")


def _dashboard_listen() -> tuple[str, int]:
    """从 ``cmd_config.json`` 读 WebUI 的 ``(scheme, port)``。

    ⚠️ 这是**容器内**的监听端口。宿主机映射成别的端口时（``-p 8080:6185``）
    这里看不出差别 —— 那种情况得用 ``localPubPublicUrl`` 手动指。
    """
    cfg_path = _data_dir() / "cmd_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return "http", 6185
    dash = cfg.get("dashboard") or {}
    try:
        port = int(dash.get("port") or 6185)
    except (TypeError, ValueError):
        port = 6185
    ssl_on = bool((dash.get("ssl") or {}).get("enable"))
    return ("https" if ssl_on else "http"), port


def _static_dir_candidates() -> list[Path]:
    """WebUI 静态根的所有候选（按优先级）。

    AstrBot 的 dist 解析有好几档（显式配置 / 用户目录 / 内置），不同版本的
    选择逻辑还不一样 —— **别猜，全试一遍，用自检挑出真能用的那个**。
    """
    out: list[Path] = []
    try:
        from astrbot.core.dashboard_assets import resolve_dashboard_dist

        d = resolve_dashboard_dist()
        if d:
            p = Path(d)
            out += [p / "dist", p]  # 有的版本返回的就是 dist 本身
    except Exception:  # noqa: BLE001
        pass
    out.append(_data_dir() / "dist")
    out += [
        Path("/AstrBot/astrbot/dashboard/dist"),
        Path("/AstrBot/dashboard/dist"),
    ]
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


async def _fetch_text(url: str, timeout: float = 8.0) -> str:
    """极简 GET（复用共享连接池）。失败抛异常。"""
    import aiohttp

    from .http import get_session

    session = get_session()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        return await resp.text()


async def _public_ip() -> str:
    """探测本机的**出口公网 IP**。

    容器走 NAT 出网，所以查到的就是宿主机的公网 IP —— 正好是我们需要的。
    """
    for url in _IP_ENDPOINTS:
        try:
            text = (await _fetch_text(url, timeout=6.0)).strip()
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r"(?:\d{1,3}\.){3}\d{1,3}", text)
        if m:
            return m.group(0)
    return ""


def _hostport(host: str, scheme: str, port: int) -> str:
    """拼 ``host`` 或 ``host:port``（默认端口不写出来）。"""
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return host
    return f"{host}:{port}"


async def _find_static_root(port: int) -> Path | None:
    """挑出「真能通过 WebUI 端口访问到」的静态根（写探针 + 拉回来比对）。"""
    for cand in _static_dir_candidates():
        try:
            if not (cand / "index.html").is_file():
                continue
            pub = cand / SUBDIR
            pub.mkdir(parents=True, exist_ok=True)
            token = f"{int(time.time())}{os.urandom(4).hex()}"
            pf = pub / f"{_PROBE_PREFIX}{token}.txt"
            pf.write_text(token, encoding="utf-8")
        except OSError:
            continue
        try:
            got = (await _fetch_text(
                f"http://127.0.0.1:{port}/{SUBDIR}/{pf.name}", timeout=8.0
            )).strip()
            ok = got == token
        except Exception:  # noqa: BLE001
            ok = False
        finally:
            try:
                pf.unlink()
            except OSError:
                pass
        if ok:
            logger.info(f"[R插件][本地直链] 静态根: {cand}")
            return cand
        logger.debug(f"[R插件][本地直链] 候选不可用: {cand}")
    return None


async def _writable_root(dir_override: str) -> Path | None:
    """拿到一个可写的静态根：优先用户指定，否则探测。"""
    if dir_override:
        root = Path(dir_override)
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except OSError as exc:
            logger.info(f"[R插件][本地直链] 目录不可用 {root}: {exc}")
            return None
    return await _find_static_root(_dashboard_listen()[1])


# ----------------------------------------------------------------------
# 目标解析（带缓存）
# ----------------------------------------------------------------------
_CACHE: PublishTarget | None = None
_CACHE_AT = 0.0
_CACHE_KEY = ""
_CACHE_OK_TTL = 1800.0   # 成功缓存 30 分钟
_CACHE_FAIL_TTL = 120.0  # 失败后 2 分钟内不再探测（别每次发图都白试）


async def resolve_target(
    *,
    base_url: str = "",
    public_url: str = "",
    directory: str = "",
    force: bool = False,
) -> PublishTarget | None:
    """解析发布目标；失败返回 ``None``（调用方退回 base64）。

    :param base_url: **自定义直链前缀**（完整）。填了完全接管，不再拼
        ``/rconsole-pub/`` —— 给「自己搭 HTTP 服务」用。
    :param public_url: **AstrBot 访问地址**（``scheme://host[:port]``）。
        只覆盖主机与端口，路径仍由插件拼。自动探测不通时填这个。
    :param directory: 落盘目录；留空走自动探测。
    :param force: 忽略缓存、也忽略熔断，强制重新探测。
    """
    global _CACHE, _CACHE_AT, _CACHE_KEY

    base_ov = normalize_base(base_url)
    pub_ov = strip_asset_path(public_url)
    dir_ov = (directory or "").strip()
    now = time.monotonic()
    key = f"{base_ov}|{pub_ov}|{dir_ov}"

    # ---- ⓪ 熔断期：直接放弃，别让用户每次发图都白等一次上传超时 ----
    if not force and is_tripped(key):
        return None

    # ---- ① 自定义前缀：用户全权接管（容器内也验证不了外部地址，不做自检）----
    if base_ov:
        _CACHE_KEY = key
        return PublishTarget(Path(dir_ov or LEGACY_DIR) / SUBDIR, base_ov)

    if not force and _CACHE_AT and key == _CACHE_KEY:
        ttl = _CACHE_OK_TTL if _CACHE is not None else _CACHE_FAIL_TTL
        if now - _CACHE_AT < ttl:
            return _CACHE

    # ---- ② 手动 AstrBot 地址 / ③ 自动探测 ----
    target = await _probe_target(pub_ov, dir_ov)
    _CACHE, _CACHE_AT, _CACHE_KEY = target, now, key
    if target is None:
        logger.info(
            "[R插件][本地直链] 没有可用的公网地址，本次退回 base64 上传"
            "（不影响发图，只是慢一点；可在插件配置里填「AstrBot 访问地址」）"
        )
    return target


async def _probe_target(public_url: str, dir_override: str) -> PublishTarget | None:
    """组装发布目标：目录 + URL 前缀。"""
    scheme, port = _dashboard_listen()

    root = await _writable_root(dir_override)
    if root is None:
        return None

    if public_url:
        # 用户给的地址说了算（可能是反代域名、或宿主映射成别的端口）
        base = f"{public_url}/{SUBDIR}"
        logger.info(f"[R插件][本地直链] 使用手动地址: {base}  ->  {root / SUBDIR}")
        return PublishTarget(root / SUBDIR, base)

    ip = await _public_ip()
    if not ip:
        logger.info("[R插件][本地直链] 探测不到公网 IP，退回 base64")
        return None

    base = f"{scheme}://{_hostport(ip, scheme, port)}/{SUBDIR}"
    logger.info(f"[R插件][本地直链] 发布目标就绪: {base}  ->  {root / SUBDIR}")
    return PublishTarget(root / SUBDIR, base)


# ----------------------------------------------------------------------
# 失败熔断：环境其实没有公网可达时，别让用户每次都白等腾讯的超时
# ----------------------------------------------------------------------
#: 连续失败几次就熔断。
#:
#: 用 2 而不是 1：单次失败可能只是抖动（腾讯侧偶发抽风），一次就熔断会误伤；
#: 而环境不对时是**每次必失败**，2 次足够认出来。
FAIL_STREAK_THRESHOLD = 2

#: 熔断时长（秒）。到点自动再试 —— 网络是波动的，不做永久禁用，
#: 免得用户换了部署环境还得重启插件。
TRIP_SECONDS = 1800.0

_TRIP_KEY = ""      # 熔断时对应的配置指纹；配置一改就自动失效
_TRIP_UNTIL = 0.0
_FAIL_STREAK = 0
_TRIP_WARNED = False


def is_tripped(key: str = "") -> bool:
    """是否处于熔断期。

    ``key`` 是配置指纹。用户改了地址相关配置后指纹会变，熔断随之自动解除 ——
    他多半正是在修这个问题，得给他立刻重试的机会。

    ``_TRIP_KEY`` 为空（还没成功探测过就失败了）时视为**对所有 key 熔断**。
    """
    if time.monotonic() >= _TRIP_UNTIL:
        return False
    return not _TRIP_KEY or not key or key == _TRIP_KEY


def note_transfer_failure() -> None:
    """记一次「腾讯取不到我们给的图」。

    **只该在 url 上传失败时调用** —— 那才是环境问题。写盘失败、探测失败都是
    本机自己的毛病，不该算进来（算了会误熔断）。连续失败到
    :data:`FAIL_STREAK_THRESHOLD` 就熔断。
    """
    global _FAIL_STREAK, _TRIP_UNTIL, _TRIP_KEY, _TRIP_WARNED
    _FAIL_STREAK += 1
    if _FAIL_STREAK < FAIL_STREAK_THRESHOLD:
        return
    _TRIP_KEY = _CACHE_KEY
    _TRIP_UNTIL = time.monotonic() + TRIP_SECONDS
    if not _TRIP_WARNED:
        _TRIP_WARNED = True
        logger.info(
            f"[R插件][本地直链] 腾讯连续 {_FAIL_STREAK} 次取不到图，"
            f"已自动停用直链 {int(TRIP_SECONDS // 60)} 分钟，改走普通上传。"
            "常见原因：这台机器没有公网 IP，或面板端口没对公网开放"
            "（家用电脑 / NAT / 内网部署都是这样）。"
            "用不上的话，把插件配置里的「官机本地图提速」关掉就不会再有这段等待。"
        )


def note_transfer_success() -> None:
    """记一次成功：清空失败计数、解除熔断。"""
    global _FAIL_STREAK, _TRIP_UNTIL, _TRIP_KEY, _TRIP_WARNED
    _FAIL_STREAK = 0
    _TRIP_UNTIL = 0.0
    _TRIP_KEY = ""
    _TRIP_WARNED = False


def failure_streak() -> int:
    """当前连续失败次数（排查 / 测试用）。"""
    return _FAIL_STREAK


def reset_cache() -> None:
    """清掉探测缓存和熔断状态（配置改了 / 测试用）。"""
    global _CACHE, _CACHE_AT, _CACHE_KEY
    _CACHE = None
    _CACHE_AT = 0.0
    _CACHE_KEY = ""
    note_transfer_success()


# ----------------------------------------------------------------------
# 写盘 / 清理
# ----------------------------------------------------------------------
def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # 原子替换：腾讯永远读不到半个文件


async def publish_to(
    data: bytes,
    target: PublishTarget,
    *,
    suffix: str = ".png",
    ttl: int = DEFAULT_TTL,
) -> str | None:
    """把字节写进发布目录，返回可访问的 URL；失败返回 ``None``。"""
    if not data:
        return None
    if len(data) > MAX_BYTES:
        logger.info(
            f"[R插件][本地直链] 文件 {len(data) // 1024}KB 超过上限 "
            f"{MAX_BYTES // 1024 // 1024}MB，不走直链"
        )
        return None

    name = content_name(data, suffix)
    path = target.directory / name
    try:
        if not (path.exists() and path.stat().st_size == len(data)):
            await asyncio.to_thread(_write, path, data)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            f"[R插件][本地直链] 写入失败（{type(exc).__name__}: {exc}），退回 base64"
        )
        return None

    if ttl and ttl > 0:
        try:
            await asyncio.to_thread(cleanup, str(target.directory), ttl)
        except Exception:  # noqa: BLE001
            pass  # 清理失败不该影响这次发送

    return target.url_for(name)


async def publish(
    data: bytes,
    *,
    suffix: str = ".png",
    base_url: str = "",
    directory: str = "",
    ttl: int = DEFAULT_TTL,
) -> str | None:
    """兼容旧签名：手填完整前缀 + 目录直接发。

    **新代码请用 :func:`resolve_target` + :func:`publish_to`。**
    """
    base = normalize_base(base_url)
    if not base or not data:
        return None
    target = PublishTarget(Path(directory or LEGACY_DIR), base)
    return await publish_to(data, target, suffix=suffix, ttl=ttl)


def cleanup(directory: str = "", ttl: int = DEFAULT_TTL) -> int:
    """删掉超过 ``ttl`` 秒没被碰过的文件，返回删除个数。

    用 mtime 判断；**已存在**的文件在 :func:`publish_to` 里不会被重写，
    所以热图会一直留到不再被引用为止。

    **只删我们自己生成的文件名**（24 位 hex + 白名单后缀 / 探针残留）——
    静态目录里还有 WebUI 的前端资源，绝不能误伤。
    """
    d = Path(directory) if directory else Path(LEGACY_DIR)
    if not d.is_dir():
        return 0
    deadline = time.time() - max(1, ttl)
    removed = 0
    for f in d.iterdir():
        try:
            if not f.is_file():
                continue
            name = f.name
            ours = name.startswith(_PROBE_PREFIX) or bool(_OUR_NAME_RE.match(name))
            if not ours:
                continue
            if f.stat().st_mtime < deadline:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
