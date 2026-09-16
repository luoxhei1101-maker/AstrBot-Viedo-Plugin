"""抖音 a-bogus 签名（调用原版 a-bogus.cjs）。

原版 ``utils/a-bogus.cjs`` 是一段逆向出来的混淆 JS（约 460 行），直接翻译成
Python 成本高、易错。这里直接在容器里用 Node 子进程调用它——AstrBot 官方
Docker 镜像自带 node（本地部署则要求系统装了 node）。

签名算法本身是纯计算的：不依赖网络、不依赖任何第三方 npm 包，输入是接口
URL 的 query 串 + User-Agent，输出一个 a_bogus 值。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from astrbot.api import logger

_CJS = Path(__file__).parent / "a_bogus.cjs"

_NODE = shutil.which("node")

# 把 cjs 路径、query 串、UA 通过 argv 传进去，避免路径里的特殊字符破坏脚本
_SCRIPT = (
    "const m = require(process.argv[1]);"
    "console.log(m.generate_a_bogus(process.argv[2], process.argv[3]));"
)


def node_available() -> bool:
    """本机是否可用 node（决定 a-bogus 能不能生成）。"""
    return _NODE is not None


async def generate_a_bogus(url_search_params: str, user_agent: str) -> str:
    """生成 a_bogus 签名值。

    Args:
        url_search_params: 抖音接口 URL 的 query 串（不含开头的 ``?``）。
        user_agent: 请求用的 User-Agent，签名会绑定它（两者必须匹配）。

    Raises:
        RuntimeError: 本机没有 node、或生成失败。
    """
    if not _NODE:
        raise RuntimeError("本机没有 node，无法生成 a-bogus 签名")

    proc = await asyncio.create_subprocess_exec(
        _NODE,
        "-e",
        _SCRIPT,
        str(_CJS),
        url_search_params,
        user_agent,
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
    logger.debug(f"[R插件][抖音] a-bogus 已生成（{len(result)} 字符）")
    return result
