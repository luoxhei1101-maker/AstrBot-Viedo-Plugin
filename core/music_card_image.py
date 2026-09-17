"""点歌列表图：把搜索结果画成一张图（不要二维码）。

为什么要画图
============

QQ 对 lightApp 卡片有强校验（需签名服务），而且一次只能展示一首、不适合放
列表。搜索结果用一张图给出来最直观：一屏能看到 10 首，用户按序号点播即可。

风格沿用已实测过的横版卡片：左侧平台色条 + 封面 + 歌名/歌手/专辑 + 平台标签。
**不含二维码**（列表场景下扫码没意义，且会挤掉信息）。

依赖 Pillow 与 Noto CJK 字体（AstrBot 官方镜像均已内置）。任一缺失时
``render_song_list`` 返回 ``None``，调用方退回文字列表，不影响主流程。
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from astrbot.api import logger

try:
    from PIL import Image, ImageDraw, ImageFont

    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

# Noto Sans CJK 的 ttc 里 index=2 是简体（SC）
FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_INDEX = 2

S = 2  # 2 倍图，QQ 里缩显更清晰
WIDTH = 400 * S

# 平台主色（也用于左侧色条与标签底色）
PLATFORM_COLORS: dict[str, tuple[int, int, int]] = {
    "netease": (194, 12, 12),
    "qqmusic": (49, 194, 124),
}
DEFAULT_COLOR = (96, 100, 110)

_font_cache: dict[tuple[str, int], Any] = {}


def _font(path: str, size: int) -> Any:
    key = (path, size)
    cached = _font_cache.get(key)
    if cached is None:
        cached = ImageFont.truetype(path, size, index=FONT_INDEX)
        _font_cache[key] = cached
    return cached


async def _download_cover(url: str, size: int, timeout: float = 10.0) -> Any:
    """下载封面并缩放到 size×size；失败返回 None。"""
    if not url:
        return None
    from .http import get_session

    try:
        session = get_session()
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件] 列表图封面下载失败 {url[:60]}: {exc}")
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return img.resize((size, size), Image.LANCZOS)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件] 列表图封面解析失败: {exc}")
        return None


async def render_song_list(
    songs: list[Any],
    keyword: str,
    platform_label: str,
    platform_key: str = "",
    hint: str = "",
) -> bytes | None:
    """画点歌列表图，返回 PNG 字节；不可用时返回 None。"""
    if not HAS_PIL or not songs:
        return None
    try:
        return await _render(songs, keyword, platform_label, platform_key, hint)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 列表图绘制失败: {type(exc).__name__}: {exc}")
        return None


async def _render(
    songs: list[Any],
    keyword: str,
    platform_label: str,
    platform_key: str,
    hint: str,
) -> bytes:
    accent = PLATFORM_COLORS.get(platform_key, DEFAULT_COLOR)

    pad = 16 * S
    head_h = 58 * S
    row_h = 74 * S
    foot_h = 34 * S if hint else 0
    height = head_h + row_h * len(songs) + foot_h + pad

    # 封面并发下载
    cover_size = 54 * S
    covers = await asyncio.gather(
        *[_download_cover(getattr(s, "cover", "") or "", cover_size) for s in songs]
    )

    canvas = Image.new("RGB", (WIDTH, height), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(
        [0, 0, WIDTH - 1, height - 1], int(11 * S),
        fill=(255, 255, 255), outline=(233, 233, 236), width=max(1, S // 2),
    )
    # 左侧平台色条
    bar_w = 5 * S
    bar = Image.new("RGB", (bar_w, height - 22 * S), accent)
    canvas.paste(bar, (max(1, S // 2), 11 * S))
    # 顶部平台色横线
    d.rectangle([pad, head_h - 2 * S, WIDTH - pad, head_h - S], fill=(240, 240, 243))

    f_head = _font(FONT_BOLD, 17 * S)
    f_head_sub = _font(FONT_REGULAR, 12 * S)
    f_idx = _font(FONT_BOLD, 14 * S)
    f_title = _font(FONT_BOLD, 15 * S)
    f_sub = _font(FONT_REGULAR, 11 * S)
    f_hint = _font(FONT_REGULAR, 11 * S)

    # ---- 头部 ----
    d.text((pad + 4 * S, 11 * S), f"点歌 · {keyword}", font=f_head, fill=(26, 26, 28))

    # 平台标签：用 anchor="mm" 让文字在胶囊里严格居中（之前手算偏移，视觉上偏）
    label_w = d.textlength(platform_label, font=f_head_sub)
    tag_x0 = pad + 4 * S
    tag_y0 = 36 * S
    tag_x1 = tag_x0 + label_w + 14 * S
    tag_y1 = tag_y0 + 19 * S
    d.rounded_rectangle([tag_x0, tag_y0, tag_x1, tag_y1], (tag_y1 - tag_y0) // 2,
                        fill=accent)
    d.text(((tag_x0 + tag_x1) / 2, (tag_y0 + tag_y1) / 2 + S // 2), platform_label,
           font=f_head_sub, fill=(255, 255, 255), anchor="mm")

    # ---- 列表 ----
    y = head_h
    num_w = 22 * S        # 序号占的宽度（放在封面之前）
    for i, (song, cover) in enumerate(zip(songs, covers), 1):
        cy = y + (row_h - cover_size) // 2
        cx = pad + 4 * S

        # 序号：封面左侧的简单数字（不叠在图上，避免遮挡封面）
        d.text((cx + num_w // 2, cy + cover_size // 2), str(i),
               font=f_idx, fill=accent, anchor="mm")
        cx += num_w

        if cover is not None:
            mask = Image.new("L", (cover_size, cover_size), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, cover_size - 1, cover_size - 1], int(7 * S), fill=255
            )
            canvas.paste(cover, (cx, cy), mask)
        else:
            d.rounded_rectangle([cx, cy, cx + cover_size, cy + cover_size],
                                int(7 * S), fill=(232, 232, 236))

        tx = cx + cover_size + 12 * S
        ty = cy + 3 * S
        title = str(getattr(song, "name", "") or "")[:26]
        d.text((tx, ty), title, font=f_title, fill=(28, 28, 30))
        ty += 19 * S
        artist = str(getattr(song, "artist", "") or "")
        album = str(getattr(song, "album", "") or "")
        sub = artist + (f" · 《{album}》" if album else "")
        d.text((tx, ty), sub[:34], font=f_sub, fill=(128, 128, 136))

        y += row_h

    # ---- 底部提示 ----
    if hint:
        d.text((WIDTH // 2, height - pad - 6 * S), hint, font=f_hint,
               fill=(150, 150, 158), anchor="md")

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
