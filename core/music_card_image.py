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
_mask_cache: dict[tuple[int, int], Any] = {}
_cover_cache: dict[tuple[str, int], Any] = {}

# 封面图缓存上限。一张 108×108 的 RGB 图约 35 KB，200 张 ≈ 7 MB ——
# 对常驻进程来说可以接受，换来的是「重复点同一首歌」时完全跳过网络。
_COVER_CACHE_MAX = 200


def _font(path: str, size: int) -> Any:
    key = (path, size)
    cached = _font_cache.get(key)
    if cached is None:
        cached = ImageFont.truetype(path, size, index=FONT_INDEX)
        _font_cache[key] = cached
    return cached


def _rounded_mask(size: int, radius: int) -> Any:
    """圆角遮罩（带缓存）。

    一屏 10 张封面的尺寸完全一样，没必要 ``Image.new`` 十次 —— 之前每张
    都新建一个 L 通道图 + 画一次圆角矩形，纯属重复劳动。
    """
    key = (size, radius)
    mask = _mask_cache.get(key)
    if mask is None:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, size - 1, size - 1], radius, fill=255
        )
        _mask_cache[key] = mask
    return mask


def _thumb_url(url: str, size: int) -> str:
    """把封面 URL 改写成平台支持的「缩略图」地址。

    网易云的图片接口支持 ``?param=宽y高`` 直接返回指定尺寸（实测有效：
    ``param=108y108`` → 实际就是 108×108）。

    **不改写的话拿到的是原图，本地再缩一次纯属浪费** —— 实测两张的差别：
    原图 545 ms / 3.9 KB，小图 204 ms / 2.8 KB；更关键的是原图那一步
    ``resize(320→108, LANCZOS)`` 在共享 CPU 上要 **338 ms/张**，
    而图已经是目标尺寸时 resize 是空操作（**8 ms**）。

    QQ 音乐的封面本来就是 ``R150x150``，不需要改。
    """
    if not url or "param=" in url:
        return url
    if "music.126.net" in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}param={size}y{size}"
    return url


def _decode_cover(data: bytes, size: int) -> Any:
    """同步「解码 + 缩放」。**调用方必须用 ``asyncio.to_thread`` 包起来。**

    别在 event loop 上直接调用它 —— 那是之前并发失效的根因（见
    ``_download_cover`` 的说明）。
    """
    img = Image.open(io.BytesIO(data))
    # draft 让 JPEG 解码器直接按 1/2^n 缩放解码，少解一大半像素（实测 2.6x）。
    # 对 PNG 是空操作；图本来就小于目标尺寸时它也不会放大。
    img.draft("RGB", (size, size))
    img = img.convert("RGB")
    if img.size != (size, size):
        # 统一到目标尺寸（**包括放大**）：paste 时用的圆角遮罩就是
        # size×size，图比遮罩小会尺寸不匹配。封面本来就极少小于 108px。
        # 走到这里尺寸已经接近目标了，BILINEAR 足够 —— 108px 的封面肉眼看不出
        # 和 LANCZOS 的差别，但在共享 CPU 上便宜得多（实测 338 ms/张 → 个位数）。
        img = img.resize((size, size), Image.BILINEAR)
    return img


async def _download_cover(url: str, size: int, timeout: float = 10.0) -> Any:
    """下载封面并缩放到 size×size；失败返回 None。

    **纯 IO 留在 event loop，解码/缩放丢进线程池。**

    之前解码是同步写在协程里的，会把 event loop 堵住 —— 结果是 10 张封面
    的并发下载变成了事实上的串行（实测 ``gather`` 4778 ms ≈ 串行 5198 ms，
    并发形同虚设）。挪进线程池后并发才真正生效。
    Pillow 的 C 扩展会释放 GIL，所以线程池里确实能并行。

    命中封面缓存时直接返回，**连网络都不碰** —— 重复点同一首歌很常见
    （搜索结果本身也有缓存），这部分省下来就是纯赚。
    """
    if not url:
        return None

    key = (url, size)
    cached = _cover_cache.get(key)
    if cached is not None:
        return cached

    from .http import get_session

    try:
        session = get_session()
        async with session.get(_thumb_url(url, size), timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件] 列表图封面下载失败 {url[:60]}: {exc}")
        return None
    try:
        img = await asyncio.to_thread(_decode_cover, data, size)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件] 列表图封面解析失败: {exc}")
        return None

    if len(_cover_cache) >= _COVER_CACHE_MAX:
        # 简单 FIFO 淘汰（dict 保序），淘汰最旧的四分之一。
        # 不做真 LRU：这里的分布是「最近搜过的歌最可能再搜」，FIFO 够用。
        for k in list(_cover_cache)[: max(1, _COVER_CACHE_MAX // 4)]:
            _cover_cache.pop(k, None)
    _cover_cache[key] = img
    return img


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
    """并发下载封面 -> 交给线程池绘制编码。

    **绘制和 PNG 编码整段是同步 CPU 活，必须丢到线程池**：画布是
    800×1696 的 2 倍图，单是 PNG 编码就要几百毫秒，留在 event loop 上
    会把这段时间里所有其它消息一起卡住。
    """
    cover_size = 54 * S
    covers = await asyncio.gather(
        *[_download_cover(getattr(s, "cover", "") or "", cover_size) for s in songs]
    )
    return await asyncio.to_thread(
        _draw_and_encode, songs, covers, keyword, platform_label, platform_key, hint
    )


def _draw_and_encode(
    songs: list[Any],
    covers: list[Any],
    keyword: str,
    platform_label: str,
    platform_key: str,
    hint: str,
) -> bytes:
    """纯同步的绘制 + 编码（调用方用 ``asyncio.to_thread`` 包起来）。"""
    accent = PLATFORM_COLORS.get(platform_key, DEFAULT_COLOR)

    pad = 16 * S
    head_h = 58 * S
    row_h = 74 * S
    foot_h = 34 * S if hint else 0
    height = head_h + row_h * len(songs) + foot_h + pad
    cover_size = 54 * S

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
            canvas.paste(cover, (cx, cy), _rounded_mask(cover_size, int(7 * S)))
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
    # 不用 optimize=True：实测画布 800×1696 时它要多吃 170 ms（480→308 ms），
    # 换来的只是 199 KB -> 202 KB（少 1.5%）—— 这点体积差异不值得那点延迟。
    canvas.save(buf, format="PNG")
    return buf.getvalue()
