"""通用链接解析适配器。

这是从 rconsole-plugin 的 ``utils/general-link-adapter.js`` 逐段移植过来的，
保留了原版的核心设计：

1. 先按平台正则把链接归一化成一个「标准形式」（短链先展开、抽 ID）；
2. 拿归一化后的链接去请求第三方解析接口；
3. 接口返回的字段名各家不一样，做统一收敛（url / images / desc）；
4. 当前接口失败就按优先级轮换下一个接口，全挂了才算失败。

原版里 `logger.mark` / `fetch` / `axios` 这些框架耦合点已经全部换掉，
业务逻辑本身几乎 1:1 保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from astrbot.api import logger

from .constants import (
    IMAGE_FIELD_NAMES,
    PARSE_ENDPOINTS,
    ParseEndpoint,
)
from .http import HttpError, expand_short_url, fetch_json

# --------------------------------------------------------------------------
# 平台专属的链接归一化
# 原版是一个个 `async ks()` / `async xigua()` 方法，这里保持同样的粒度
# --------------------------------------------------------------------------


KUAISHOU_LONG_RE = re.compile(
    r"(?:https?://)?(?:www|v)\.(?:kuaishou|m\.chenzhongtech)\.com/[A-Za-z\d._?%&+\-=\/#]*",
    re.IGNORECASE,
)
XIGUA_RE = re.compile(
    r"(?:https?://)?(?:www|v|m)\.ixigua\.com/[A-Za-z\d._?%&+\-=\/#]*",
    re.IGNORECASE,
)
PIPIXIA_RE = re.compile(r"https://h5\.pipix\.com/(?:s|item|ppx/item)/[A-Za-z0-9_-]+")
PIPIGX_RE = re.compile(r"https://h5\.pipigx\.com/pp/post/[A-Za-z0-9]+")
QQ_XSJ_RE = re.compile(r"https://s\.xsj\.qq\.com/[A-Za-z0-9]+")
TIEBA_RE = re.compile(r"https://tieba\.baidu\.com/p/[A-Za-z0-9]+")
JIKE_RE = re.compile(r"https://m\.okjike\.com/originalPosts/[A-Za-z0-9]+")
DOUYIN_RE = re.compile(r"(?:http://|https://)v\.douyin\.com/[A-Za-z\d._?%&+\-=\/#]*")


@dataclass
class NormalizedTarget:
    """平台归一化之后的中间结果。"""

    platform: str
    """平台中文名。"""

    rule_key: str
    """平台配置 key。"""

    target_url: str
    """归一化后的链接，用来拼第三方接口。"""


class NormalizeError(RuntimeError):
    """链接归一化失败（正则没抠出东西、或是没见过的形式）。"""


async def _normalize_kuaishou(link: str) -> NormalizedTarget:
    m = KUAISHOU_LONG_RE.search(link)
    if not m:
        raise NormalizeError("无法从链接中提取快手信息")
    url = m.group(0)

    # 短链先展开（原版：msg.includes("v.kuaishou") 时 fetch 一次拿重定向）
    if "v.kuaishou" in url:
        url = await expand_short_url(url)

    if "/fw/photo/" in url:
        video_id = re.search(r"/fw/photo/([^/?]+)", url)
    elif "/fw/long-video/" in url:
        # QQ 分享卡片里的长视频形式
        video_id = re.search(r"/fw/long-video/([^/?]+)", url)
    elif "short-video" in url:
        video_id = re.search(r"short-video/([^/?]+)", url)
    else:
        raise NormalizeError("无法提取快手的信息，请重试或者换一个视频")

    if not video_id:
        raise NormalizeError("无法提取快手视频 ID")

    return NormalizedTarget(
        platform="快手",
        rule_key="kuaishou",
        target_url=f"https://www.kuaishou.com/short-video/{video_id.group(1)}",
    )


async def _normalize_ixigua(link: str) -> NormalizedTarget:
    m = XIGUA_RE.search(link)
    if not m:
        raise NormalizeError("无法从链接中提取西瓜视频信息")
    url = m.group(0)

    if "v.ixigua" in url:
        url = await expand_short_url(url)

    id_match = re.search(r"ixigua\.com/(\d+)", url) or re.search(r"/video/(\d+)", url)
    if not id_match:
        raise NormalizeError("无法提取西瓜视频 ID")

    return NormalizedTarget(
        platform="西瓜",
        rule_key="ixigua",
        target_url=f"https://www.ixigua.com/{id_match.group(1)}",
    )


async def _normalize_pipixia(link: str) -> NormalizedTarget:
    m = PIPIXIA_RE.search(link)
    if not m:
        raise NormalizeError("无法提取皮皮虾链接")
    return NormalizedTarget("皮皮虾", "pipixia", m.group(0))


async def _normalize_pipigx(link: str) -> NormalizedTarget:
    m = PIPIGX_RE.search(link)
    if not m:
        raise NormalizeError("无法提取皮皮搞笑链接")
    return NormalizedTarget("皮皮搞笑", "pipigx", m.group(0))


async def _normalize_qq_xsj(link: str) -> NormalizedTarget:
    m = QQ_XSJ_RE.search(link)
    if not m:
        raise NormalizeError("无法提取 QQ 小世界链接")
    return NormalizedTarget("QQ小世界", "qq_xsj", m.group(0))


async def _normalize_tieba(link: str) -> NormalizedTarget:
    m = TIEBA_RE.search(link)
    if not m:
        raise NormalizeError("无法提取贴吧链接")
    return NormalizedTarget("贴吧", "tieba", m.group(0))


async def _normalize_jike(link: str) -> NormalizedTarget:
    m = JIKE_RE.search(link)
    if not m:
        raise NormalizeError("无法提取即刻链接")
    return NormalizedTarget("即刻", "jike", m.group(0))


async def _normalize_douyin(link: str) -> NormalizedTarget:
    m = DOUYIN_RE.search(link)
    if not m:
        raise NormalizeError("无法提取抖音链接")
    return NormalizedTarget("抖音动图", "douyin_gif", m.group(0))


_NORMALIZERS = {
    "kuaishou": _normalize_kuaishou,
    "ixigua": _normalize_ixigua,
    "pipixia": _normalize_pipixia,
    "pipigx": _normalize_pipigx,
    "qq_xsj": _normalize_qq_xsj,
    "tieba": _normalize_tieba,
    "jike": _normalize_jike,
    "douyin_gif": _normalize_douyin,
}


# --------------------------------------------------------------------------
# 解析结果
# --------------------------------------------------------------------------


@dataclass
class ParseResult:
    platform: str
    success: bool
    video: str | None = None
    images: list[str] = field(default_factory=list)
    desc: str = ""
    endpoint_sign: int | None = None
    error: str | None = None

    @property
    def has_media(self) -> bool:
        return bool(self.video or self.images)


# --------------------------------------------------------------------------
# 适配器主体
# --------------------------------------------------------------------------


class GeneralLinkAdapter:
    """第三方解析接口适配器，用于大面积覆盖解析视频内容。"""

    def __init__(self, endpoints: tuple[ParseEndpoint, ...] | list[ParseEndpoint] | None = None):
        self.endpoints = list(endpoints or PARSE_ENDPOINTS)

    @staticmethod
    def is_api_response_success(data: dict) -> bool:
        """判断接口返回是否算成功。

        对应原版 ``isApiResponseSuccess``：code 命中失败列表，或者 data 里
        既没有 url 也没有任何图片字段，都判定为失败。
        """
        fail_codes = {-2, 400, 404, -1, 500}
        code = data.get("code")
        try:
            if code is not None and int(code) in fail_codes:
                return False
        except (TypeError, ValueError):
            pass

        payload = data.get("data")
        if not isinstance(payload, dict):
            return False

        if payload.get("url") is not None:
            return True
        if payload.get("playAddr") is not None:
            return True
        return any(payload.get(name) for name in IMAGE_FIELD_NAMES)

    @staticmethod
    def normalize_payload(data: dict) -> tuple[str | None, list[str], str]:
        """把各家不一样的返回字段收敛成 (视频, 图片列表, 文案)。

        对应原版 resolve() 里那串套娃三元表达式，这里改成顺序断言，
        可读性好一点，行为一致。
        """
        payload = data.get("data") or {}

        video = payload.get("url") or payload.get("playAddr")

        images: list[str] = []
        for name in IMAGE_FIELD_NAMES:
            value = payload.get(name)
            if value:
                images = value if isinstance(value, list) else [value]
                break

        desc = payload.get("title") or payload.get("desc") or ""
        return video, images, desc

    async def resolve_with_endpoint(
        self,
        endpoint: ParseEndpoint,
        target: NormalizedTarget,
    ) -> ParseResult:
        """用指定接口解析一次。"""
        req_url = endpoint.build(target.target_url)
        logger.debug(f"[R插件移植版][通用解析] 平台: {target.platform}, 接口 #{endpoint.sign}: {req_url}")

        data = await fetch_json(req_url, timeout=15.0)

        if not self.is_api_response_success(data):
            return ParseResult(
                platform=target.platform,
                success=False,
                endpoint_sign=endpoint.sign,
                error=str(data.get("msg") or "接口返回失败"),
            )

        video, images, desc = self.normalize_payload(data)
        return ParseResult(
            platform=target.platform,
            success=True,
            video=video,
            images=images,
            desc=desc,
            endpoint_sign=endpoint.sign,
        )

    async def parse(self, link: str, rule_key: str) -> ParseResult | None:
        """完整流程：归一化 -> 首选接口 -> 失败则轮换。

        Returns:
            解析结果；如果这个链接压根不归通用适配器管，返回 None。
        """
        normalizer = _NORMALIZERS.get(rule_key)
        if not normalizer:
            return None

        try:
            target = await normalizer(link)
        except NormalizeError as exc:
            return ParseResult(
                platform=rule_key,
                success=False,
                error=f"链接归一化失败: {exc}",
            )

        last: ParseResult | None = None
        for idx, endpoint in enumerate(self.endpoints):
            try:
                result = await self.resolve_with_endpoint(endpoint, target)
            except HttpError as exc:
                logger.warning(f"[R插件移植版][通用解析] 接口 #{endpoint.sign} 请求异常: {exc}")
                last = ParseResult(
                    platform=target.platform,
                    success=False,
                    endpoint_sign=endpoint.sign,
                    error=str(exc),
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 单个接口炸掉不能影响轮换
                logger.warning(f"[R插件移植版][通用解析] 接口 #{endpoint.sign} 解析异常: {exc}")
                last = ParseResult(
                    platform=target.platform,
                    success=False,
                    endpoint_sign=endpoint.sign,
                    error=str(exc),
                )
                continue

            if result.success and result.has_media:
                logger.info(
                    f"[R插件移植版][通用解析] {target.platform} 解析成功，"
                    f"使用接口 #{endpoint.sign}（第 {idx + 1}/{len(self.endpoints)} 个）"
                )
                return result

            last = result

        logger.warning(f"[R插件移植版][通用解析] {target.platform} 所有接口均无法解析")
        return last or ParseResult(
            platform=target.platform,
            success=False,
            error="所有接口均无法解析",
        )
