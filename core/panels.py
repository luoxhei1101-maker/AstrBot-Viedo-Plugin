"""三张状态图的绘制：#R菜单 / #cookie状态 / #服务状态。

统一风格：随机背景 + 毛玻璃面板 + 白色文字。所有布局都在「先算高度、
再画」的两段式里完成 —— Pillow 不支持自适应排版，高度必须自己算准，
否则不是留白就是出界。

菜单内容**从 ``core/constants.py`` 的规则表生成**，不手写清单：
以后加了平台/指令，菜单自动跟着变，不会出现「文档说支持、实际没有」的漂移。
"""

from __future__ import annotations

from typing import Any

from .render_image import (
    ACCENT,
    DIM_COLOR,
    TEXT_COLOR,
    TITLE_COLOR,
    render_to_png,
)

PAD = 26
# 每条「指令 + 说明」占的高度。**必须够高**：两行文字（25px 名 + 20px 说明）
# 加内边距要 80px 以上，太小说明文字会掉到圆角框外面（踩过）。
CMD_H = 86
# 纯文字行
LINE_H = 34
SECTION_TITLE_H = 48
SECTION_GAP = 14
HEADER_H = 152
FOOTER_H = 62


# ----------------------------------------------------------------------
# 布局：先算总高，再画
# ----------------------------------------------------------------------


class _Layout:
    """极简布局器：按顺序往里塞块，最后知道总高度。"""

    def __init__(self) -> None:
        self.blocks: list[tuple[str, Any, int]] = []
        self.h = PAD + HEADER_H

    def header(self, title: str, subtitle: str, right: str = "") -> None:
        self.blocks.append(("header", (title, subtitle, right), HEADER_H))

    def profile(self, avatar: Any, title: str, subtitle: str, extra: str = "") -> None:
        """带头像的资料块（给服务状态页用）。"""
        self.blocks.append(("profile", (avatar, title, subtitle, extra), 196))

    def section(self, title: str) -> None:
        self.blocks.append(("section", title, SECTION_TITLE_H))

    def cmd(self, name: str, desc: str, color: tuple = ACCENT) -> None:
        self.blocks.append(("cmd", (name, desc, color), CMD_H))

    def line(self, text: str, color: tuple = TEXT_COLOR, size: int = 23) -> None:
        self.blocks.append(("line", (text, color, size), LINE_H))

    def gap(self, h: int = SECTION_GAP) -> None:
        self.blocks.append(("gap", None, h))

    def footer(self, text: str) -> None:
        self.blocks.append(("footer", text, FOOTER_H))

    @property
    def total(self) -> int:
        return self.h + sum(b[2] for b in self.blocks) + PAD

    def draw(self, panel: Any) -> None:
        """把块画到画布上。"""
        w = panel.size[0]
        y = PAD

        for kind, payload, h in self.blocks:
            if kind == "header":
                title, subtitle, right = payload
                panel.glass((PAD, y, w - PAD, y + HEADER_H - 18), radius=30, alpha=138)
                panel.text((PAD + 30, y + 24), title, 40, True, TITLE_COLOR)
                panel.text((PAD + 31, y + 76), subtitle, 22, False, DIM_COLOR)
                if right:
                    panel.right(w - PAD - 28, y + 32, right, 22, False, DIM_COLOR)
                y += h

            elif kind == "section":
                # 左侧一根竖色条 + 标题。注意 accent_bar 的第二个参数是颜色，
                # 别把标题字符串传进去（踩过：ValueError: unknown color specifier）
                panel.accent_bar((PAD + 4, y + 12, PAD + 11, y + 38), ACCENT, width=7)
                panel.text((PAD + 24, y + 6), payload, 27, True, TITLE_COLOR)
                y += h

            elif kind == "profile":
                avatar, title, subtitle, extra = payload
                box = (PAD, y, w - PAD, y + h - 18)
                panel.glass(box, radius=30, alpha=138)
                size = 112
                ax, ay = box[0] + 30, box[1] + 26
                panel.paste_avatar(avatar, ax, ay, size)
                tx = ax + size + 26
                panel.text(
                    (tx, ay + 4), panel.fit(title, 32, w - tx - PAD - 30, True),
                    32, True, (126, 231, 165),
                )
                panel.text(
                    (tx, ay + 46), panel.fit(subtitle, 23, w - tx - PAD - 30),
                    23, False, TEXT_COLOR,
                )
                if extra:
                    panel.text(
                        (tx, ay + 78), panel.fit(extra, 21, w - tx - PAD - 30),
                        21, False, DIM_COLOR,
                    )
                y += h

            elif kind == "cmd":
                name, desc, color = payload
                box = (PAD, y, w - PAD, y + CMD_H - 12)
                panel.glass(box, radius=18, alpha=138)
                panel.accent_bar(
                    (box[0] + 10, box[1] + 12, box[0] + 15, box[3] - 12), color, width=5
                )
                # 名（25px，占 ~34px）在上，说明（20px，占 ~26px）在下
                panel.text(
                    (box[0] + 28, box[1] + 10),
                    panel.fit(name, 25, w - 2 * PAD - 56, True), 25, True, color,
                )
                panel.text(
                    (box[0] + 28, box[1] + 46),
                    panel.fit(desc, 20, w - 2 * PAD - 56), 20, False, DIM_COLOR,
                )
                y += h

            elif kind == "line":
                text, color, size = payload
                panel.text(
                    (PAD + 16, y + 2), panel.fit(text, size, w - 2 * PAD - 32),
                    size, False, color,
                )
                y += h

            elif kind == "bar":
                # 进度条：左边标签、右边数值，下面一条细槽
                label, pct, text, color = payload
                x0 = PAD + 22
                x1 = w - PAD - 22
                panel.text((x0, y + 2), label, 23, False, TEXT_COLOR)
                panel.right(x1, y + 2, text, 23, False, color)
                bar_y = y + LINE_H + 2
                panel.draw.rounded_rectangle(
                    [x0, bar_y, x1, bar_y + 8], 4, fill=(255, 255, 255, 62)
                )
                ratio = max(0.0, min(1.0, float(pct) / 100.0))
                filled = int((x1 - x0) * ratio)
                if filled > 3:
                    panel.draw.rounded_rectangle(
                        [x0, bar_y, x0 + filled, bar_y + 8], 4, fill=color
                    )
                y += h

            elif kind == "footer":
                panel.center(w // 2, y + 8, payload, 21, False, DIM_COLOR)
                y += h

            else:  # gap
                y += h


# ----------------------------------------------------------------------
# 菜单
# ----------------------------------------------------------------------

# 指令的「给人看」的用法与说明。key 对应 COMMAND_RULES 里的 key，
# 没写到的会自动用规则表里的 name 兜底。
_CMD_DOC: dict[str, tuple[str, str]] = {
    "musicSearch": ("点歌 <歌名>", "搜歌；# 内回复序号播放，也可 <平台>点歌"),
    "musicPick": ("（发序号）", "点歌列表发出后 60 秒内回数字即播放"),
    "biliScan": ("#RBQ", "B站扫码登录，自动写入 Cookie"),
    "biliState": ("#RBS", "查看 B站登录状态"),
    "neteaseScan": ("#RNQ", "网易云扫码登录（只需这一步就能点歌听 VIP）"),
    "cookieStatus": ("#cookie状态", "查所有 Cookie 是否失效 + 会员等级"),
    "serviceStatus": ("#服务状态", "查服务器负载与 Bot 信息"),
    "rMenu": ("#R菜单", "就是这张图"),
}

# 自动解析的展示名（按 AUTO_RULES 里的顺序过滤）
_AUTO_HIDE = {"linkShareSummary"}


def _fmt_names(names: list[str], per_line: int = 5) -> list[str]:
    lines: list[str] = []
    for i in range(0, len(names), per_line):
        lines.append("　·　".join(names[i : i + per_line]))
    return lines


def build_menu_layout(version: str = "", bot_name: str = "") -> _Layout:
    from .constants import AUTO_RULES, COMMAND_RULES

    lay = _Layout()
    lay.header("R插件 · 功能菜单", "发链接自动解析 · 点歌 · 状态查询",
               f"v{version}" if version else "")

    # ---- 一、自动解析 ----
    auto = [r.name for r in AUTO_RULES if r.enabled and r.key not in _AUTO_HIDE]
    lay.section("链接解析（无需指令，发链接即触发）")
    for ln in _fmt_names(auto):
        lay.line(ln, TEXT_COLOR, 22)
    lay.gap()

    # ---- 二、点歌 ----
    lay.section("点歌")
    lay.cmd("点歌 <歌名>", "按配置的默认平台搜歌", ACCENT)
    lay.cmd("网易云点歌 <歌名>", "只搜网易云（匿名可用，配 Cookie 可听 VIP）", (255, 138, 138))
    lay.cmd("QQ点歌 <歌名>", "只搜 QQ 音乐（需 Cookie 才能取完整音源）", (126, 231, 165))
    lay.cmd("（接着发 1 / 2 / 3）", "60 秒内回序号即播放，序号一次性有效", (255, 209, 128))
    lay.gap()

    # ---- 三、状态与查询 ----
    lay.section("状态与查询")
    order = ("cookieStatus", "serviceStatus", "neteaseScan", "biliScan",
             "biliState", "rMenu")
    rules = {r["key"]: r for r in COMMAND_RULES}
    for key in order:
        if key in rules:
            name, desc = _CMD_DOC.get(key, (rules[key]["name"], rules[key]["name"]))
            color = (255, 209, 128) if rules[key].get("admin") else ACCENT
            lay.cmd(name, desc, color)
    lay.gap()

    # ---- 四、用法提示 ----
    lay.section("使用教程")
    for text in (
        "1. 发链接：群里直接粘贴抖音/B站/快手…链接，自动解析内容",
        "2. 点歌：发「点歌 歌名」→ 得到列表图 → 回复序号播放",
        "3. Cookie：在插件配置里按平台填；B站/网易云可直接扫码",
        "4. 换发送方式：插件配置「点歌」分组 → 发送方式",
        "5. 管理员指令（标黄色的）只有 AstrBot 管理员可用",
    ):
        lay.line(text, DIM_COLOR, 22)

    lay.footer(f"共 {len(AUTO_RULES)} 类平台 · {len(COMMAND_RULES)} 条指令"
               + (f" · {bot_name}" if bot_name else ""))
    return lay


async def render_menu(version: str = "", bot_name: str = "", bg_api: str = "") -> bytes | None:
    lay = build_menu_layout(version, bot_name)
    return await render_to_png(lay.draw, lay.total, bg_api)


# ----------------------------------------------------------------------
# Cookie 状态
# ----------------------------------------------------------------------


def build_cookie_layout(rows: list[dict], checked_at: str = "") -> _Layout:
    """已配置的平台逐条列出；未配置的合并成一行 —— 用户要的是「简略」。"""
    lay = _Layout()
    configured = [r for r in rows if r.get("status") != "off"]
    missing = [r for r in rows if r.get("status") == "off"]
    ok = sum(1 for r in configured if r.get("status") == "ok")

    lay.header(
        "Cookie 状态",
        f"已配置 {len(configured)} 个 · 可用 {ok} 个 · 共 {len(rows)} 个平台",
        checked_at[:16],
    )

    if configured:
        lay.section("已配置")
        for r in configured:
            lay.cmd(
                f"{r.get('label', '')}　{r.get('nickname') or ''}".rstrip(),
                r.get("detail") or "",
                _Layout_color(r.get("status", "off")),
            )
        lay.gap()

    if missing:
        lay.section("未配置")
        for ln in _fmt_names([r.get("label", "") for r in missing], 4):
            lay.line(ln, DIM_COLOR, 22)

    lay.footer("状态：可用 / 已失效 / 已配置（无法校验）")
    return lay


def _Layout_color(status: str) -> tuple:
    return {
        "ok": (126, 231, 165),
        "bad": (255, 138, 138),
        "warn": (255, 209, 128),
        "off": DIM_COLOR,
    }.get(status, TEXT_COLOR)


async def render_cookie_status(rows: list[dict], checked_at: str = "",
                               bg_api: str = "") -> bytes | None:
    lay = build_cookie_layout(rows, checked_at)
    return await render_to_png(lay.draw, lay.total, bg_api)


# ----------------------------------------------------------------------
# 服务状态
# ----------------------------------------------------------------------


def _bar(lay: _Layout, label: str, value: float, text: str, color: tuple) -> None:
    """加一条「标签 + 数值 + 进度槽」的行（高度额外多 14px 留给进度槽）。"""
    lay.blocks.append(("bar", (label, value, text, color), LINE_H + 14))


async def render_service_status(info: dict, bg_api: str = "") -> bytes | None:
    lay = _Layout()
    lay.header("服务状态", info.get("host", ""), info.get("time", "")[:16])

    # ---- Bot（带头像） ----
    lay.section("Bot")
    lay.profile(
        info.get("avatar"),
        f"QQ　{info.get('bot_qq', '未知')}",
        f"昵称　{info.get('bot_name') or '—'}",
        f"平台　{info.get('platform', '—')}　·　"
        f"已启用解析　{info.get('enabled_platforms', '—')}",
    )
    lay.cmd(
        "插件版本",
        f"v{info.get('version', '?')}　·　命令 {info.get('cmd_count', 0)} 条"
        f"　·　识别 {info.get('auto_count', 0)} 类",
        (255, 209, 128),
    )
    lay.gap()

    # ---- 负载 ----
    lay.section("系统负载")
    for item in info.get("loads", []):
        _bar(lay, item["label"], item["percent"], item["text"],
             _bar_color(item["percent"]))
    lay.gap()

    # ---- 运行环境 ----
    lay.section("运行环境")
    for text in info.get("env_lines", []):
        lay.line(text, DIM_COLOR, 22)

    lay.footer(info.get("footer", ""))
    return await render_to_png(lay.draw, lay.total, bg_api)


def _bar_color(pct: float) -> tuple:
    if pct >= 85:
        return (255, 138, 138)
    if pct >= 60:
        return (255, 209, 128)
    return (126, 231, 165)
