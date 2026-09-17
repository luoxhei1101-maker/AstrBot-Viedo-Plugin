"""抖音 a-bogus 签名（调用原版 a-bogus.cjs）。

原版 ``utils/a-bogus.cjs`` 是一段逆向出来的混淆 JS（约 460 行），直接翻译成
Python 成本高、易错。这里直接在容器里用 Node 子进程调用它——AstrBot 官方
Docker 镜像自带 node（本地部署则要求系统装了 node）。

签名算法本身是纯计算的：不依赖网络、不依赖任何第三方 npm 包，输入是接口
URL 的 query 串 + User-Agent，输出一个 a_bogus 值。

**性能：常驻 worker（重要）**
=============================

早期实现是「一次签名 = 起一个 node 子进程」::

    node -e "const m=require('a_bogus.cjs');console.log(m.generate_a_bogus(...))"

这条路的开销分布极不均衡：node 冷启动 40–80ms、``require`` 解析执行 460 行
混淆 JS 再花 40–120ms，**而签名本身不到 5ms**。也就是说 95% 以上的时间
纯粹花在「把引擎热起来」上，而且还签一次热一次，永远热不起来。

现在改为**常驻 worker**（``a_bogus_worker.cjs``）：进程只起一次、脚本只
``require`` 一次，之后走 stdin/stdout 行协议复用同一个 V8 实例，
单次签名降到亚毫秒级。

**降级**：worker 起不来（node 缺失、脚本损坏、容器限制）时，自动回退到
原来的「一次性子进程」方式，功能不受影响——只是慢一点。两条路都失败才抛错。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from astrbot.api import logger

_CJS = Path(__file__).parent / "a_bogus.cjs"
_WORKER = Path(__file__).parent / "a_bogus_worker.cjs"

_NODE = shutil.which("node")

# 一次性调用用的兜底脚本
_SCRIPT = (
    "const m = require(process.argv[1]);"
    "console.log(m.generate_a_bogus(process.argv[2], process.argv[3]));"
)

# 单次签名的等待上限。正常是亚毫秒级，给 10s 已经极其宽松——
# 超时说明 worker 卡死，直接杀掉重来比无限等更合理。
_SIGN_TIMEOUT = 10.0

# 启动 worker 的等待上限（冷启动 + require 混淆脚本）
_READY_TIMEOUT = 20.0


def node_available() -> bool:
    """本机是否可用 node（决定 a-bogus 能不能生成）。"""
    return _NODE is not None


class _Worker:
    """常驻 node 签名进程。进程内串行处理请求（有锁），避免响应错位。"""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._broken = False
        self._starting = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> bool:
        """确保 worker 在跑。成功返回 True，起不来返回 False（标记 broken）。"""
        if self._broken:
            return False
        if self._proc is not None and self._proc.returncode is None:
            return True
        if not _NODE or not _WORKER.is_file():
            self._broken = True
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                _NODE,
                str(_WORKER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][抖音] a-bogus worker 启动失败: {exc}")
            self._broken = True
            return False

        # 读就绪行，确认脚本真的加载成功（而不是起来就死）
        try:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=_READY_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("[R插件][抖音] a-bogus worker 就绪超时，改用一次性调用")
            await self._kill(proc)
            self._broken = True
            return False

        if not line:
            stderr = b""
            try:
                stderr = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                f"[R插件][抖音] a-bogus worker 启动即退出: "
                f"{stderr.decode('utf-8', errors='ignore')[:200]}"
            )
            self._broken = True
            return False

        try:
            hello = json.loads(line.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            hello = {}

        if not hello.get("ready"):
            # 脚本加载失败时 worker 会回 {"ok": false, "e": ...}
            logger.warning(
                f"[R插件][抖音] a-bogus worker 不可用: {hello.get('e', '未知原因')}"
            )
            await self._kill(proc)
            self._broken = True
            return False

        self._proc = proc
        logger.info("[R插件][抖音] a-bogus worker 已启动（常驻签名进程）")
        return True

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """结束一个子进程，尽力而为。"""
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
        except Exception:  # noqa: BLE001 - 清理失败不该抛
            pass

    async def close(self) -> None:
        """插件卸载时关停 worker。"""
        proc, self._proc = self._proc, None
        if proc is not None:
            await self._kill(proc)
            logger.debug("[R插件][抖音] a-bogus worker 已关停")

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------

    async def sign(self, query: str, ua: str) -> str | None:
        """用常驻进程签名。失败返回 None（调用方走降级）。"""
        if not await self._ensure_started():
            return None

        # 串行化：worker 是单进程逐行处理，并发写会让响应和请求错位
        async with self._lock:
            proc = self._proc
            if proc is None or proc.returncode is not None:
                return None
            try:
                payload = json.dumps({"q": query, "ua": ua}) + "\n"
                proc.stdin.write(payload.encode("utf-8"))
                await proc.stdin.drain()

                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=_SIGN_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件][抖音] a-bogus worker 通信失败，将重建: {exc}")
                await self._kill(proc)
                self._proc = None
                return None

        if not line:
            # worker 死了（EOF）。下次调用会自动重建，本次走降级
            logger.warning("[R插件][抖音] a-bogus worker 意外退出，将重建")
            if proc.returncode is not None:
                self._proc = None
            return None

        try:
            resp = json.loads(line.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            logger.warning("[R插件][抖音] a-bogus worker 返回了非 JSON")
            return None

        if not resp.get("ok"):
            logger.warning(f"[R插件][抖音] a-bogus 生成失败: {resp.get('e')}")
            return None

        value = str(resp.get("v") or "").strip()
        return value or None


_worker = _Worker()


async def close_worker() -> None:
    """插件卸载 / 重载时关停常驻 node 进程。"""
    await _worker.close()


async def _generate_once(query: str, ua: str) -> str:
    """降级路径：起一个一次性 node 子进程生成签名。"""
    proc = await asyncio.create_subprocess_exec(
        _NODE,
        "-e",
        _SCRIPT,
        str(_CJS),
        query,
        ua,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"a-bogus 生成失败: {stderr.decode('utf-8', errors='ignore')[:200]}"
        )

    result = stdout.decode("utf-8", errors="ignore").strip()
    if not result:
        raise RuntimeError("a-bogus 生成了空值")
    return result


async def generate_a_bogus(url_search_params: str, user_agent: str) -> str:
    """生成 a_bogus 签名值。

    优先走常驻 worker（亚毫秒级）；worker 不可用时回退到一次性子进程。

    Args:
        url_search_params: 抖音接口 URL 的 query 串（不含开头的 ``?``）。
        user_agent: 请求用的 User-Agent，签名会绑定它（两者必须匹配）。

    Raises:
        RuntimeError: 本机没有 node、或两条路都生成失败。
    """
    if not _NODE:
        raise RuntimeError("本机没有 node，无法生成 a-bogus 签名")

    value = await _worker.sign(url_search_params, user_agent)
    if value:
        logger.debug(f"[R插件][抖音] a-bogus 已生成（{len(value)} 字符，常驻进程）")
        return value

    # 降级：一次性子进程。这条路的错误要抛出去（上层据此判断 Cookie/node 状态）
    value = await _generate_once(url_search_params, user_agent)
    logger.debug(f"[R插件][抖音] a-bogus 已生成（{len(value)} 字符，一次性进程）")
    return value
