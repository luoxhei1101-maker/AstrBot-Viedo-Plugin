"""图片命令的离线测试：#R菜单 / #cookie状态 / #服务状态。

分两层，能跑多少跑多少：

- **本机**（没装 Pillow）：命令正则匹配、花体字母折叠、布局高度合理性
- **容器内**（有 Pillow + Noto CJK）：额外真跑一遍三张图的渲染，
  断言出的是合法 PNG 且体积正常

所以这个文件直接丢进容器执行，验证的就是**已部署的那份代码**::

    docker exec astrbot python <插件目录>/tests/test_panels.py
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent))


def _real_astrbot_available() -> bool:
    try:
        import astrbot.api.event  # noqa: F401
        import astrbot.api.message_components  # noqa: F401
        import astrbot.api.star  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


NEED_STUBS = not _real_astrbot_available()

if NEED_STUBS and "astrbot" not in sys.modules:
    _logger = types.SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    _api = types.ModuleType("astrbot.api")
    _api.logger = _logger
    _api.AstrBotConfig = dict
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       期望: {want!r}")
        print(f"       实际: {got!r}")
        FAILED.append(name)


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


# ======================================================================
# 1. 命令规则
# ======================================================================
def rule_checks() -> None:
    print("=" * 70)
    print("1. 命令规则匹配")
    print("=" * 70)

    from astrbot_plugin_rconsole.core.constants import COMMAND_RULES

    rules = {r["key"]: r for r in COMMAND_RULES}
    for key in ("neteaseScan", "cookieStatus", "serviceStatus", "rMenu"):
        check_true(f"COMMAND_RULES 里有 {key}", key in rules)

    cases: list[tuple[str, str, bool]] = [
        # (规则 key, 输入, 是否应命中)
        ("neteaseScan", "#RNQ", True),
        ("neteaseScan", "#rnq", True),
        ("neteaseScan", "#网易云扫码", True),
        ("neteaseScan", "#RBQ", False),
        ("cookieStatus", "#cookie状态", True),
        ("cookieStatus", "cookie状态", True),
        ("cookieStatus", "#Cookie状态", True),
        ("cookieStatus", "#ck状态", True),
        ("cookieStatus", "#点歌 晴天", False),
        ("serviceStatus", "#服务状态", True),
        ("serviceStatus", "服务状态", True),
        ("serviceStatus", "#bot状态", True),
        ("serviceStatus", "#服务器状态", True),
        ("rMenu", "#R菜单", True),
        ("rMenu", "R菜单", True),
        ("rMenu", "#r菜单", True),
        ("rMenu", "#R帮助", True),
        ("rMenu", "#R功能", True),
        # ⚠️ 裸词与 `#菜单` 都必须**不**命中（2026-09-26 用户要求）：
        # `^#?` 让前缀可选，列进裸词后「@机器人 + 菜单」就会抢别的插件的事件。
        ("rMenu", "#菜单", False),
        ("rMenu", "菜单", False),
        ("rMenu", "菜单功能", False),
        ("rMenu", "#点歌", False),
    ]
    for key, text, want in cases:
        pat = rules[key]["pattern"]
        got = bool(re.search(pat, text, re.I | re.M))
        check(f"{key}: {text!r} -> {got}（期望 {want}）", got, want)

    # 管理员权限：只有网易云扫码是管理员专属
    check("neteaseScan 需要管理员", rules["neteaseScan"]["admin"], True)
    for key in ("cookieStatus", "serviceStatus", "rMenu"):
        check(f"{key} 不需要管理员", rules[key]["admin"], False)

    # 处理函数必须登记在 _LOCAL_COMMAND_METHODS 里（否则点了没反应）
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    for handler in ("netease_scan", "cookie_status", "service_status", "r_menu"):
        check_true(
            f"main.py 的 _LOCAL_COMMAND_METHODS 登记了 {handler}",
            f'"{handler}":' in main_src,
        )


# ======================================================================
# 2. 花体字母折叠 + 缺字处理
# ======================================================================
def text_checks() -> None:
    print()
    print("=" * 70)
    print("2. 花体字母折叠（QQ 昵称常见）")
    print("=" * 70)

    from astrbot_plugin_rconsole.core.render_image import fold_styled

    cases = [
        ("𝓝𝓪𝓲𝓛𝓾𝓸", "NaiLuo"),          # 花体（用户真实的 QQ 昵称）
        ("𝐇𝐞𝐥𝐥𝐨", "Hello"),             # 粗体
        ("𝑊𝑜𝑟𝑙𝑑", "World"),             # 斜体
        ("ℕ𝕚𝕔𝕖", "Nice"),               # 双线体
        ("𝖂𝖔𝖗𝖑𝖉", "World"),             # 粗花体
        ("𝚃𝚎𝚡𝚝", "Text"),               # 等宽
        ("𝟏𝟐𝟑", "123"),                # 粗体数字
        ("普通中文", "普通中文"),            # 中文不动
        ("ABC xyz", "ABC xyz"),         # ASCII 不动
        ("", ""),
    ]
    for src, want in cases:
        got = fold_styled(src)
        check(f"{src!r} -> {got!r}", got, want)

    # 缺字时不崩：私用区 / emoji 都应被替换成 ·（而不是画出豆腐块）
    try:
        from astrbot_plugin_rconsole.core.render_image import HAS_PIL, clean_text

        if HAS_PIL:
            check("私用区字符被替换（无豆腐块）", clean_text("A\ue000B", 24), "A·B")
            check("emoji 被替换（Noto CJK 无彩色 emoji）",
                  clean_text("A\U0001f600B", 24), "A·B")
            check("正常中英数不受影响",
                  clean_text("网易云 VIP Lv.7", 24), "网易云 VIP Lv.7")
            check("花体字母先折叠再检查",
                  clean_text("𝓝𝓪𝓲𝓛𝓾𝓸", 24), "NaiLuo")
        else:
            print("  (跳过 clean_text：本机没装 Pillow)")
    except Exception as exc:  # noqa: BLE001
        print(f"  (clean_text 检查跳过: {exc})")


# ======================================================================
# 3. 布局高度
# ======================================================================
def layout_checks() -> None:
    print()
    print("=" * 70)
    print("3. 布局高度合理性")
    print("=" * 70)

    from astrbot_plugin_rconsole.core.panels import (
        CMD_H,
        LINE_H,
        build_cookie_layout,
        build_menu_layout,
    )

    menu = build_menu_layout("1.5.0", "测试Bot")
    h = menu.total
    check_true(f"菜单高度合理（{h}px）", 600 <= h <= 4000)
    # 每条 cmd 都要真的给它 CMD_H 的高度，否则说明文字会溢出面板
    check_true("CMD_H 足够容纳两行文字（>=80）", CMD_H >= 80)
    check_true("LINE_H 合理", 24 <= LINE_H <= 60)

    rows = [
        {"platform": "netease", "label": "网易云音乐", "status": "ok",
         "nickname": "幕Ming", "detail": "黑胶VIP Lv.7"},
        {"platform": "douyin", "label": "抖音", "status": "off",
         "nickname": "", "detail": "未配置"},
        {"platform": "bili", "label": "哔哩哔哩", "status": "bad",
         "nickname": "", "detail": "已失效"},
    ]
    cook = build_cookie_layout(rows, "2026-09-17 22:00")
    check_true(f"Cookie 面板高度合理（{cook.total}px）", 300 <= cook.total <= 2000)
    # 未配置的应该合并成文字行，而不是一条条 cmd
    kinds = [b[0] for b in cook.blocks]
    check("Cookie 面板：已配置走 cmd、未配置走 line",
          kinds.count("cmd"), 2)   # ok + bad
    check_true("Cookie 面板含 line 行（未配置合并）", kinds.count("line") >= 1)

    # 每条 cmd 的高度必须与 CMD_H 一致，否则算出来和画出来会对不上
    cmd_heights = {b[2] for b in menu.blocks if b[0] == "cmd"}
    check("菜单里所有 cmd 行高一致且等于 CMD_H", cmd_heights, {CMD_H})


# ======================================================================
# 4. 真实渲染（需要 Pillow）
# ======================================================================
def render_checks() -> None:
    print()
    print("=" * 70)
    print("4. 真实渲染（需要 Pillow + Noto CJK）")
    print("=" * 70)

    try:
        from astrbot_plugin_rconsole.core.render_image import HAS_PIL
    except Exception as exc:  # noqa: BLE001
        print(f"  (跳过：导入失败 {exc})")
        return

    if not HAS_PIL:
        print("  (跳过：本机没装 Pillow，容器里有)")
        return

    # 光有 Pillow 还不够 —— 渲染要中文字体，而 Noto CJK 只在容器镜像里。
    # 本机（Windows）没有这个字体时渲染必然失败，那属于「环境不具备」，
    # 跳过而不是判定失败，否则每次在本机跑都是假红。
    try:
        from astrbot_plugin_rconsole.core.render_image import font as _probe_font

        _probe_font(24)
    except Exception as exc:  # noqa: BLE001
        print(f"  (跳过：本机缺中文字体，容器里有 —— {type(exc).__name__}: {exc})")
        print("        想在本机验渲染：docker exec astrbot python "
              ".../tests/test_panels.py")
        return

    import asyncio

    from astrbot_plugin_rconsole.core.panels import (
        render_cookie_status,
        render_menu,
        render_service_status,
    )

    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

    async def run() -> None:
        # 不传 bg_api -> 用纯色底，测试不依赖外网
        menu = await render_menu("1.5.0", "测试Bot", "")
        check_true("菜单能渲染", menu and len(menu) > 5000)
        check("菜单是 PNG", menu[:8] if menu else b"", PNG_MAGIC)

        rows = [
            {"platform": "netease", "label": "网易云音乐", "status": "ok",
             "nickname": "幕Ming", "detail": "黑胶VIP Lv.7　到期 2027-10-31"},
            {"platform": "qqmusic", "label": "QQ音乐", "status": "ok",
             "nickname": "𝓝𝓪𝓲𝓛𝓾𝓸", "detail": "豪华绿钻　Lv5"},
            {"platform": "douyin", "label": "抖音", "status": "warn",
             "nickname": "", "detail": "已配置（无校验接口）"},
            {"platform": "bili", "label": "哔哩哔哩", "status": "off",
             "nickname": "", "detail": "未配置"},
        ]
        cookie = await render_cookie_status(rows, "2026-09-17 22:00", "")
        check_true("Cookie 面板能渲染", cookie and len(cookie) > 3000)
        check("Cookie 面板是 PNG", cookie[:8] if cookie else b"", PNG_MAGIC)

        info = {
            "host": "test-host", "time": "2026-09-17 22:00:00",
            "bot_qq": "10001", "bot_name": "测试Bot", "avatar": None,
            "platform": "aiocqhttp", "enabled_platforms": "20 个",
            "version": "1.5.0", "cmd_count": 9, "auto_count": 24,
            "loads": [
                {"label": "CPU（4 核）", "percent": 31.0, "text": "31%"},
                {"label": "内存", "percent": 74.0, "text": "2.8GB / 3.8GB"},
            ],
            "env_lines": ["运行时长　1小时", "Python　3.12.14"],
            "footer": "测试",
        }
        svc = await render_service_status(info, "")
        check_true("服务状态能渲染（无头像也不崩）", svc and len(svc) > 3000)
        check("服务状态是 PNG", svc[:8] if svc else b"", PNG_MAGIC)

    asyncio.run(run())


def main() -> int:
    rule_checks()
    text_checks()
    layout_checks()
    render_checks()
    print()
    print("=" * 70)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for n in FAILED:
            print("   -", n)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
