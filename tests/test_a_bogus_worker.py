"""测试 a_bogus 常驻 worker：正确性 + 耗时对比。

不需要 AstrBot 环境也能跑（a_bogus.py 只依赖 astrbot.api.logger，
这里用 stub 顶掉）。
"""
import asyncio
import sys
import time
import types
from pathlib import Path

# ---- stub astrbot.api.logger ----
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def _p(self, *a):
        pass
    info = debug = warning = error = _p


api.logger = _Logger()
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)

PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])
if PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, PLUGIN_PARENT)

from astrbot_plugin_rconsole.core import a_bogus  # noqa: E402

QUERY = (
    "device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1"
    "&version_code=170400&version_name=17.4.0&cookie_enabled=true"
    "&screen_width=1536&screen_height=864&browser_language=zh-CN"
    "&browser_platform=Win32&browser_name=Chrome&browser_version=123.0.0.0"
    "&browser_online=true&engine_name=Blink&engine_version=123.0.0.0"
    "&os_name=Windows&os_version=10&cpu_core_num=16&device_memory=8"
    "&platform=PC&downlink=10&effective_type=4g&round_trip_time=50"
    "&webid=7362810250930783783"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

failed = []


def check(name, cond, extra=""):
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        failed.append(name)


async def main():
    print("--- 1. node 可用性 ---")
    check("node_available() 为真", a_bogus.node_available())

    print("\n--- 2. 常驻 worker 正确性 ---")
    v1 = await a_bogus.generate_a_bogus(QUERY, UA)
    check("首次签名非空", bool(v1), f"{len(v1)} 字符" if v1 else "")
    check("签名以 = 结尾（原版格式）", v1.endswith("=") if v1 else False)

    v2 = await a_bogus.generate_a_bogus(QUERY, UA)
    check("二次签名非空", bool(v2))
    # getRandomStr 每次都不同，所以两次结果不同是正常的
    check("两次签名均有效（长度一致）", len(v1) == len(v2), f"{len(v1)} vs {len(v2)}")

    print("\n--- 3. 耗时对比：常驻 vs 一次性子进程 ---")
    # 常驻（已热）
    n = 5
    t0 = time.perf_counter()
    for _ in range(n):
        await a_bogus.generate_a_bogus(QUERY, UA)
    warm = (time.perf_counter() - t0) / n

    # 一次性子进程（走内部降级函数）
    t0 = time.perf_counter()
    for _ in range(n):
        await a_bogus._generate_once(QUERY, UA)
    once = (time.perf_counter() - t0) / n

    print(f"  常驻 worker: {warm * 1000:.1f} ms/次")
    print(f"  一次性进程:  {once * 1000:.1f} ms/次")
    print(f"  提速:        {once / warm:.1f}x")
    check("常驻明显快于一次性", warm < once, f"{once / warm:.1f}x")

    print("\n--- 4. worker 关停 ---")
    await a_bogus.close_worker()
    check("close_worker 后进程已清空", a_bogus._worker._proc is None)

    # 关停后应能自动重建（不标记 broken）
    v3 = await a_bogus.generate_a_bogus(QUERY, UA)
    check("关停后可自动重建并签名", bool(v3))
    await a_bogus.close_worker()

    print()
    if failed:
        print(f"共 {len(failed)} 项失败: {failed}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
