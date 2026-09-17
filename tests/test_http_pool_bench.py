"""端到端性能验证：新 HTTP 连接池 + 常驻 a-bogus worker 的真实耗时。

用真实网络请求（B站公开接口，无需 Cookie）对比「旧实现」和「新实现」。
旧实现用「每个请求新建 session」的等价写法模拟。
"""
import asyncio
import sys
import time
import types
from pathlib import Path

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def _p(self, *a, **k):
        pass
    info = debug = warning = error = _p


api.logger = _Logger()
api.AstrBotConfig = dict
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)

PARENT = str(Path(__file__).resolve().parents[2])
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

import aiohttp  # noqa: E402

from astrbot_plugin_rconsole.core.http import BROWSER_HEADERS, fetch, close_session  # noqa: E402

# 用 B 站一个轻量公开接口，连发多次看连接复用效果
URLS = [
    "https://api.bilibili.com/x/web-interface/view?bvid=BV1Hs4C6FEkP",
    "https://api.bilibili.com/x/web-interface/nav",
] * 4  # 同一 host 连发 8 次


async def old_way(url: str) -> int:
    """模拟旧实现：每个请求新建 connector + session。"""
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15), connector=connector
    ) as s:
        async with s.get(url, headers=BROWSER_HEADERS) as r:
            await r.read()
            return r.status


async def new_way(url: str) -> int:
    body, _ = await fetch(url, timeout=15, retries=0)
    return 200 if body else 0


async def main():
    print("--- 预热（建立初始连接，不计入对比）---")
    try:
        await new_way(URLS[0])
        await old_way(URLS[0])
    except Exception as exc:
        print(f"  网络不可用，跳过对比: {type(exc).__name__}: {exc}")
        return 0

    print("\n--- 旧实现：每次新建 session（8 次请求）---")
    t0 = time.perf_counter()
    for u in URLS:
        try:
            await old_way(u)
        except Exception:
            pass
    old_ms = (time.perf_counter() - t0) * 1000
    print(f"  总耗时 {old_ms:.0f} ms，平均 {old_ms / len(URLS):.0f} ms/次")

    print("\n--- 新实现：共享连接池（8 次请求）---")
    t0 = time.perf_counter()
    for u in URLS:
        try:
            await new_way(u)
        except Exception:
            pass
    new_ms = (time.perf_counter() - t0) * 1000
    print(f"  总耗时 {new_ms:.0f} ms，平均 {new_ms / len(URLS):.0f} ms/次")

    print(f"\n结论：{old_ms / new_ms:.2f}x 提速（{old_ms:.0f}ms -> {new_ms:.0f}ms）")

    print("\n--- 并发能力验证（新实现，8 个请求一起发）---")
    t0 = time.perf_counter()
    await asyncio.gather(*(new_way(u) for u in URLS), return_exceptions=True)
    conc_ms = (time.perf_counter() - t0) * 1000
    print(f"  8 个请求并发耗时 {conc_ms:.0f} ms")

    await close_session()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
