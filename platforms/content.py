"""文本类 resolver：AI 链接总结、翻译。

原版这两块的能力来源不一样：
- ``linkShareSummary``：抓网页正文 -> 丢给自建 OpenAI（``utils/llm-util.js``）
- ``trans``：走 DeepL / 百度等翻译引擎（``utils/trans-strategy.js``）

移植后统一改成 **走 AstrBot 自己的 LLM Provider**，好处是用户不用再单独填
一份 baseURL / apiKey / model，直接用机器人已经配好的模型。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from ..core.constants import SUMMARY_PROMPT, TRANS_MAP
from ..core.http import HttpError, fetch
from .base import ResolveResult, ResolverContext, register

# 只留正文，去掉 script/style/nav 这类噪声
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|svg|iframe)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# 正文长度上限，避免把整个页面塞进 LLM 把 token 烧穿
_MAX_CONTENT_CHARS = 6000


def html_to_text(html: str) -> str:
    """HTML 转纯文本。"""
    text = _SCRIPT_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = _WS_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


async def fetch_page_text(url: str) -> str:
    """抓网页并转成正文文本。"""
    body, _ = await fetch(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        retries=1,
        timeout=25.0,
    )
    return html_to_text(body.decode("utf-8", errors="ignore"))


@register("link_summary")
async def resolve_link_summary(link: str, ctx: ResolverContext) -> ResolveResult:
    """抓取链接正文并让 LLM 总结。"""
    # 命令形式「#总结一下 <链接>」里抠出真正的 URL
    m = re.search(r"https?://\S+", link)
    if not m:
        return ResolveResult.fail("AI总结", "没有找到有效的链接")
    target = m.group(0).rstrip("）)】]")

    try:
        text = await fetch_page_text(target)
    except HttpError as exc:
        return ResolveResult.fail("AI总结", f"网页抓取失败: {exc}")

    if len(text) < 120:
        return ResolveResult.fail("AI总结", "网页正文太短，可能是动态渲染页面或需要登录")

    clipped = text[:_MAX_CONTENT_CHARS]
    prompt = SUMMARY_PROMPT.replace("{content}", clipped)

    try:
        answer = await ctx.llm(prompt)
    except Exception as exc:  # noqa: BLE001 - LLM 失败原因很多，统一兜底
        logger.error(f"[R插件][AI总结] LLM 调用失败: {exc}")
        return ResolveResult.fail("AI总结", f"LLM 调用失败: {exc}")

    if not answer or not answer.strip():
        return ResolveResult.fail("AI总结", "LLM 没有返回内容（可能没配置模型）")

    return ResolveResult.ok(
        "AI总结",
        desc=answer.strip(),
        extra={"source_url": target, "content_chars": len(clipped), "text_only": True},
    )


_TRANS_RE = re.compile(r"^(?:翻|trans)([" + "".join(TRANS_MAP) + r"])\s*([\s\S]*)$")


@register("trans")
async def resolve_trans(link: str, ctx: ResolverContext) -> ResolveResult:
    """翻译。

    触发形式（对应原版 rule 的 ``^(翻|trans)[语种]``）：
    - ``翻en hello world``
    - ``transjp こんにちは``

    原版走的是 DeepL 等翻译引擎，这里改成 LLM —— 质量不差，还省掉一份 API Key。
    """
    m = _TRANS_RE.match(link.strip())
    if not m:
        return ResolveResult.fail("翻译", "格式应为「翻<语种> <内容>」，例如「翻en hello」")

    lang_code, content = m.group(1), m.group(2).strip()
    if not content:
        return ResolveResult.fail("翻译", "没有要翻译的内容")

    lang_name = TRANS_MAP.get(lang_code, lang_code)
    prompt = (
        f"请把下面的内容翻译成{lang_name}，"
        f"只输出译文本身，不要任何解释、前缀或引号：\n\n{content[:3000]}"
    )

    try:
        answer = await ctx.llm(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][翻译] LLM 调用失败: {exc}")
        return ResolveResult.fail("翻译", f"LLM 调用失败: {exc}")

    if not answer or not answer.strip():
        return ResolveResult.fail("翻译", "LLM 没有返回内容（可能没配置模型）")

    return ResolveResult.ok(
        "翻译",
        desc=f"{answer.strip()}",
        extra={"target_lang": lang_name, "text_only": True},
    )
