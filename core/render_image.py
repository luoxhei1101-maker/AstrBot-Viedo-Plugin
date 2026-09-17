"""图片渲染基础设施：随机背景 + 毛玻璃面板。

给「#R菜单 / #cookie状态 / #服务状态」三个图片命令共用。

设计要点
========

**背景来自随机图 API**（默认 ``https://api.elaina.cat/random/``，返回 jpeg）。
实测服务器拉一次约 **2.5 秒**，所以：

1. 调用方应该**先起 ``fetch_background()`` 的 task**，再去并行取其它数据
   （Cookie 校验、系统信息、头像…），最后再 ``await`` 它 —— 否则这 2.5 秒
   完全是白等。
2. 内部做了**同一时刻只发一次请求**的去重（in-flight 共享），外加极短的
   TTL 缓存，避免连发命令时把公益接口打爆。

**毛玻璃的实现方式**：把整张背景缩到 1/4、高斯模糊、再放大回目标尺寸，
然后面板只是盖一层半透明白圆角矩形。这样只需**一次**模糊（而不是每个面板
单独裁切模糊），实测比逐面板裁切快好几倍，视觉上没有区别（本来就是重模糊）。

**中文字体**：AstrBot 官方镜像内置 Noto Sans CJK，``ttc`` 里 ``index=2``
是简体（SC）。字体对象带缓存，重复渲染不会反复读盘。

所有 Pillow 计算都放在 ``asyncio.to_thread`` 里，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any

from astrbot.api import logger

try:
    from PIL import Image, ImageDraw, ImageFont

    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

FONT_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_INDEX = 2

DEFAULT_BG_API = "https://api.elaina.cat/random/"

# 背景抓取超时；超过就退化成纯色底（不能让命令卡死）
BG_TIMEOUT = 12.0
# 同一次「命令风暴」内复用同一张背景（秒）。0 = 每次真的重新拉。
BG_CACHE_TTL = 5.0

# 画布与配色
CANVAS_W = 760
RADIUS = 26
# 深色面板的不透明度（0-255）。太低会看见背景细节、文字发花；
# 太高就没「毛玻璃」的通透了。118 是实测比较平衡的值。
PANEL_ALPHA = 118

TITLE_COLOR = (255, 255, 255)
TEXT_COLOR = (240, 244, 250)
DIM_COLOR = (196, 206, 220)
OK_COLOR = (126, 231, 165)
BAD_COLOR = (255, 138, 138)
WARN_COLOR = (255, 209, 128)
ACCENT = (146, 190, 255)

_font_cache: dict[tuple[str, int], Any] = {}
_bg_lock: asyncio.Lock | None = None
_bg_cache: tuple[float, Any] | None = None
_bg_inflight: asyncio.Future | None = None
_glyph_cache: dict[tuple[str, int, str], bool] = {}


# ----------------------------------------------------------------------
# 文本清理：花体字母 + 缺字
# ----------------------------------------------------------------------
#
# QQ 昵称大量使用「数学花体/粗体」等 Unicode 变体字母（例如 ``𝓝𝓪𝓲𝓛𝓾𝓸``），
# Noto Sans CJK **没有**这些字形，直接画会全是豆腐块（□）。实测就是这样。
#
# 处理顺序：
# 1. ``fold_styled()`` 先把这些变体字母折回 ASCII（``𝓝𝓪𝓲𝓛𝓾𝓸`` -> ``NaiLuo``），
#    这样昵称还能认出来，比直接丢掉好得多；
# 2. ``clean_text()`` 再逐字检查字体里到底有没有这个字形，没有就丢掉，
#    避免任何漏网的方块。

# 数学字母数字符号区里，每个「字母表」的 A 的码点；每个占 52 位（A-Z + a-z）
_MATH_ALPHA_STARTS = (
    0x1D400, 0x1D434, 0x1D468, 0x1D49C, 0x1D4D0, 0x1D504, 0x1D538,
    0x1D56C, 0x1D5A0, 0x1D5D4, 0x1D608, 0x1D63C, 0x1D670,
)
# 同上，数字 0-9 各占 10 位
_MATH_DIGIT_STARTS = (0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6)

# fraktur / double-struck 等字母表里，个别字母被放在「字母式符号」区，
# 需要单独映射，否则会漏
_LETTERLIKE = {
    0x2102: "C", 0x210A: "g", 0x210B: "H", 0x210C: "H", 0x210D: "H",
    0x210E: "h", 0x2110: "I", 0x2111: "I", 0x2112: "L", 0x2113: "l",
    0x2115: "N", 0x2119: "P", 0x211A: "Q", 0x211B: "R", 0x211C: "R",
    0x211D: "R", 0x2124: "Z", 0x2128: "Z", 0x212C: "B", 0x212D: "C",
    0x212F: "e", 0x2130: "E", 0x2131: "F", 0x2133: "M", 0x2134: "o",
}


def fold_styled(text: str) -> str:
    """把数学粗体/花体等变体字母折回普通 ASCII。"""
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if cp in _LETTERLIKE:
            out.append(_LETTERLIKE[cp])
            continue
        hit = ""
        for start in _MATH_ALPHA_STARTS:
            off = cp - start
            if 0 <= off < 52:
                hit = chr(ord("A") + off) if off < 26 else chr(ord("a") + off - 26)
                break
        if hit:
            out.append(hit)
            continue
        for start in _MATH_DIGIT_STARTS:
            off = cp - start
            if 0 <= off < 10:
                hit = str(off)
                break
        out.append(hit if hit else ch)
    return "".join(out)


def _mask_bytes(f: Any, ch: str) -> bytes | None:
    """取某个字符的位图字节。

    ⚠️ Pillow 12 里 ``FreeTypeFont.getmask()`` 返回的是 **``ImagingCore``**，
    它**没有 ``tobytes()``**（早期版本返回 Image 才有）。所以要么直接调，
    要么用 ``Image.Image()._new(core)`` 包一层再取。实测两种路径都要兜。
    """
    try:
        mask = f.getmask(ch)
    except Exception:  # noqa: BLE001
        return None
    tobytes = getattr(mask, "tobytes", None)
    if callable(tobytes):
        try:
            return tobytes()
        except Exception:  # noqa: BLE001
            pass
    try:
        from PIL import Image as _Image

        return _Image.Image()._new(mask).tobytes()
    except Exception:  # noqa: BLE001
        return None


def _has_glyph(f: Any, ch: str, key: tuple[str, int, str]) -> bool:
    """字体里是否真有这个字形。

    缺字会渲染成**豆腐块**（.notdef，实测是 40×40 的方框），拿私用区字符
    当基准比对位图；比不出来（异常）时保守认为是有的，不误删正常文字。
    """
    cached = _glyph_cache.get(key)
    if cached is not None:
        return cached

    notdef = _mask_bytes(f, "\ue000")
    current = _mask_bytes(f, ch)
    if notdef is None or current is None:
        ok = True
    else:
        ok = current != notdef
    _glyph_cache[key] = ok
    return ok


def clean_text(text: str, size: int, bold: bool = False) -> str:
    """折花体 + 丢掉字体里没有的字形。"""
    if not text:
        return ""
    text = fold_styled(text)
    f = font(size, bold)
    out: list[str] = []
    for ch in text:
        if ch in " \t":
            out.append(ch)
            continue
        if _has_glyph(f, ch, (font_path(bold), size, ch)):
            out.append(ch)
        else:
            out.append("·")
    return "".join(out)


def font_path(bold: bool = False) -> str:
    return FONT_BOLD if bold else FONT_REGULAR



def font(size: int, bold: bool = False) -> Any:
    """取一个中文字体对象（带缓存）。"""
    path = FONT_BOLD if bold else FONT_REGULAR
    key = (path, size)
    cached = _font_cache.get(key)
    if cached is None:
        cached = ImageFont.truetype(path, size, index=FONT_INDEX)
        _font_cache[key] = cached
    return cached


# ----------------------------------------------------------------------
# 背景
# ----------------------------------------------------------------------


async def _download_bg(api: str) -> Any:
    from .http import get_session

    try:
        session = get_session()
        async with session.get(
            api,
            timeout=BG_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*"},
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[R插件] 随机背景接口 HTTP {resp.status}，改用纯色底")
                return None
            raw = await resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 随机背景下载失败: {type(exc).__name__}: {exc}")
        return None

    def _decode() -> Any:
        return Image.open(io.BytesIO(raw)).convert("RGB")

    try:
        return await asyncio.to_thread(_decode)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 随机背景解析失败: {type(exc).__name__}: {exc}")
        return None


async def fetch_background(api: str = "") -> Any:
    """拉一张随机背景。失败返回 ``None``（调用方用纯色底兜住）。

    并发去重 + 极短 TTL 缓存，避免连发命令时把公益接口打爆。
    """
    global _bg_lock, _bg_cache, _bg_inflight

    url = (api or DEFAULT_BG_API).strip()
    if not url or not url.startswith("http"):
        return None
    if not HAS_PIL:
        return None

    now = time.time()
    if BG_CACHE_TTL > 0 and _bg_cache is not None:
        ts, img = _bg_cache
        if now - ts < BG_CACHE_TTL:
            return img

    if _bg_lock is None:
        _bg_lock = asyncio.Lock()

    async with _bg_lock:
        # 等锁期间可能已经被别人拉好了
        if BG_CACHE_TTL > 0 and _bg_cache is not None:
            ts, img = _bg_cache
            if time.time() - ts < BG_CACHE_TTL:
                return img
        img = await _download_bg(url)
        _bg_cache = (time.time(), img)
        return img


# ----------------------------------------------------------------------
# 画布
# ----------------------------------------------------------------------


def _cover_blur(bg: Any, size: tuple[int, int], blur: int = 22) -> Any:
    """把背景 cover 裁剪到目标尺寸，并做「缩小→模糊→放大」得到毛玻璃底。

    在 1/4 尺度上模糊再放大，比原尺寸直接模糊快得多，而重模糊下观感一致。
    """
    w, h = size
    if bg is None:
        return Image.new("RGB", (w, h), (32, 38, 52))

    bw, bh = bg.size
    scale = max(w / bw, h / bh)
    nw, nh = max(w, int(bw * scale + 0.5)), max(h, int(bh * scale + 0.5))
    resized = bg.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    cropped = resized.crop((left, top, left + w, top + h))

    small = cropped.resize((max(1, w // 4), max(1, h // 4)), Image.BILINEAR)
    from PIL import ImageFilter

    blurred = small.filter(ImageFilter.GaussianBlur(blur / 4))
    base = blurred.resize((w, h), Image.BILINEAR)

    # 压一层暗色，保证白字始终清晰
    overlay = Image.new("RGB", (w, h), (10, 14, 24))
    return Image.blend(base, overlay, 0.34)


class Panel:
    """一张画布 + 常用绘制助手。"""

    def __init__(self, bg: Any, size: tuple[int, int], blur: int = 22) -> None:
        self.size = size
        self.canvas = _cover_blur(bg, size, blur)
        self.draw = ImageDraw.Draw(self.canvas, "RGBA")
        self.y = 0

    # ---- 面板 ----

    def glass(self, box: tuple[int, int, int, int], radius: int = RADIUS,
              alpha: int = PANEL_ALPHA) -> None:
        """半透明**深色**圆角面板 + 一圈细高光边。

        用深色而不是白色：背景被压暗后是中间调，白字落在**白色**面板上对比度
        很差（实测「说明文字」几乎看不清）。深色面板才是「毛玻璃 + 文字清晰」
        的正确组合。
        """
        x0, y0, x1, y1 = box
        self.draw.rounded_rectangle(
            [x0, y0, x1, y1], radius, fill=(14, 18, 30, alpha)
        )
        self.draw.rounded_rectangle(
            [x0, y0, x1, y1], radius, outline=(255, 255, 255, 52), width=1
        )

    def accent_bar(self, box: tuple[int, int, int, int], color: tuple[int, int, int],
                   width: int = 6) -> None:
        x0, y0, x1, y1 = box
        self.draw.rounded_rectangle([x0, y0, x0 + width, y1], width // 2, fill=color)

    def paste_avatar(self, img: Any, x: int, y: int, size: int) -> None:
        """把头像裁成圆形贴上去；没有头像时画一个占位圆。"""
        if img is None:
            self.draw.ellipse(
                [x, y, x + size - 1, y + size - 1], fill=(255, 255, 255, 58)
            )
            self.draw.text(
                (x + size // 2, y + size // 2), "无",
                font=font(int(size * 0.34)), fill=DIM_COLOR, anchor="mm",
            )
            return
        avatar = img.convert("RGBA").resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        self.canvas.paste(avatar, (x, y), mask)
        self.draw.ellipse(
            [x, y, x + size - 1, y + size - 1], outline=(255, 255, 255, 160), width=2
        )

    # ---- 文本 ----

    def text(self, xy: tuple[int, int], s: str, size: int = 26,
             bold: bool = False, fill: tuple = TEXT_COLOR) -> None:
        self.draw.text(xy, clean_text(s, size, bold), font=font(size, bold), fill=fill)

    def center(self, cx: int, y: int, s: str, size: int = 26,
               bold: bool = False, fill: tuple = TEXT_COLOR) -> None:
        self.draw.text((cx, y), clean_text(s, size, bold),
                       font=font(size, bold), fill=fill, anchor="ma")

    def right(self, rx: int, y: int, s: str, size: int = 26,
              bold: bool = False, fill: tuple = TEXT_COLOR) -> None:
        self.draw.text((rx, y), clean_text(s, size, bold),
                       font=font(size, bold), fill=fill, anchor="ra")

    def fit(self, s: str, size: int, max_w: int, bold: bool = False) -> str:
        """折花体、丢缺字，再按像素宽度截断（超出补 ``…``）。"""
        s = clean_text(s, size, bold)
        f = font(size, bold)
        if self.draw.textlength(s, font=f) <= max_w:
            return s
        ell = "…"
        ell_w = self.draw.textlength(ell, font=f)
        out = ""
        for ch in s:
            if self.draw.textlength(out + ch, font=f) + ell_w > max_w:
                break
            out += ch
        return out + ell

    # ---- 输出 ----

    def to_png(self) -> bytes:
        buf = io.BytesIO()
        # compress_level 调低换速度（默认 6）—— 图片要发到 QQ，体积本来就不小，
        # 这里优先保证「点一下很快就出图」
        self.canvas.save(buf, format="PNG", compress_level=3)
        return buf.getvalue()

    @staticmethod
    def color_for(status: str) -> tuple[int, int, int]:
        return {
            "ok": OK_COLOR,
            "bad": BAD_COLOR,
            "warn": WARN_COLOR,
            "off": DIM_COLOR,
        }.get(status, TEXT_COLOR)


async def make_panel(height: int, bg_api: str = "", blur: int = 22):
    """拉背景并建一张画布（Pillow 部分在线程里跑）。"""
    if not HAS_PIL:
        return None
    bg = await fetch_background(bg_api)
    return await asyncio.to_thread(Panel, bg, (CANVAS_W, height), blur)


async def render_to_png(builder, height: int, bg_api: str = "",
                        blur: int = 22) -> bytes | None:
    """通用入口：拉背景 → 建画布 → 交给 ``builder(panel)`` 画 → 出 PNG。

    ``builder`` 是同步函数（在 to_thread 里跑）。任何异常都吞掉并返回
    ``None``，调用方退回文字输出。
    """
    if not HAS_PIL:
        return None
    try:
        bg = await fetch_background(bg_api)
        panel = await asyncio.to_thread(Panel, bg, (CANVAS_W, height), blur)
        await asyncio.to_thread(builder, panel)
        return await asyncio.to_thread(panel.to_png)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 图片渲染失败: {type(exc).__name__}: {exc}")
        return None
