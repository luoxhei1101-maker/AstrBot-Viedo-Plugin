"""Cookie 组装。

原插件的 Cookie 配置是**要求用户自己拼格式**的——yaml 注释里写得很清楚：

    抖音  douyinCookie: 格式：odin_tt=xxx;passport_fe_beating_status=xxx;sid_guard=xxx;...
    微博  weiboCookie:  格式：_T_WM=xxx; WEIBOCN_FROM=xxx; MLOGIN=xxx; XSRF-TOKEN=xxx; ...
    酷狗  kugouCookie:  格式：token=xxx;userid=xxx;dfid=xxx
    小黑盒 xiaoheiheCookie: 格式：x_xhh_tokenid=xxx

让用户从浏览器里挑着复制、再手动拼 ``key=value; key=value``，很容易漏分号、
漏空格，漏一个就解析失败，而且失败信息还看不出来是哪儿错了。

这个模块把「拼格式」这件事从用户身上拿走：

- **想省事**：逐个填对应字段，这里按标准格式组装
- **已经有一整段**：直接粘进「整段 Cookie」，优先级最高，原样透传

拆解方案（哪些 key、什么顺序、哪些是必需的）放在 ``cookie_spec.py``，
那份是纯数据，转换脚本也要读它来生成配置 schema。
"""

from __future__ import annotations

from astrbot.api import logger

from .cookie_spec import COOKIE_SPECS, CookieSpec, get_spec

__all__ = ["COOKIE_SPECS", "CookieSpec", "build_cookie", "get_spec", "describe"]


def build_cookie(platform: str, conf_get) -> str:
    """组装某个平台的 Cookie。

    Args:
        platform: 平台 key（如 ``douyin`` / ``bili`` / ``kuaishou``）。
        conf_get: 读配置的函数，签名 ``conf_get(path, default) -> value``。

    Returns:
        可直接放进请求头的 Cookie 字符串；两条路都没填时返回空串。

    优先级：
        1. 「整段 Cookie」填了就用它（原样透传，不做任何加工）
        2. 否则把逐项填写的字段按 ``key=value; key=value`` 拼起来
    """
    spec = get_spec(platform)
    if spec is None:
        return ""

    raw = str(conf_get(spec.raw_path, "") or "").strip()
    if raw:
        # 已经是完整串（含 =）就直接用
        if "=" in raw:
            return raw

        # 存的是单值（B站 SESSDATA / 小黑盒 token），补上 key 名
        if spec.raw_is_single and spec.raw_key:
            return f"{spec.raw_key}={raw}"

        # 不是单值语义却填了个裸值，说明用户填错了。提醒但仍然透传，
        # 不擅自改用户给的字符串。
        logger.warning(
            f"[R插件][Cookie] {spec.label} 的「整段 Cookie」里没有 '='，"
            f"看起来不像完整 Cookie，已原样使用"
        )
        return raw

    # ---- 逐项拼装 ----
    parts: list[str] = []
    for key in spec.keys:
        value = str(conf_get(f"{spec.fields_path}.{key}", "") or "").strip()
        if value:
            parts.append(f"{key}={value}")

    if not parts:
        return ""

    assembled = "; ".join(parts)

    # 对着 spec 里的必需项做个体检，缺了就在日志里点名，别让用户猜
    required = spec.required_any
    if required:
        provided = {p.split("=", 1)[0] for p in parts}
        if not (set(required) & provided):
            logger.warning(
                f"[R插件][Cookie] {spec.label} 逐项填写的 Cookie 缺少关键项"
                f"（{' 或 '.join(required)}），解析可能会失败"
            )

    logger.debug(f"[R插件][Cookie] {spec.label} 已由 {len(parts)} 个字段拼装完成")
    return assembled


def describe() -> str:
    """生成一段 Cookie 配置自检报告，插件加载时打日志用。

    用户最常踩的坑就是"填了 Cookie 但格式不对"，启动时直接说清楚状态，
    比让他去翻文档强。
    """
    lines: list[str] = []
    for spec in COOKIE_SPECS:
        lines.append(f"{spec.label}({len(spec.keys)} 项)")
    return "、".join(lines)
