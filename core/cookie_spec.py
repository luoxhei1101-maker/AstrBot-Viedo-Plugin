"""Cookie 拆解方案（纯数据，不依赖 AstrBot）。

单独成一个模块的原因：转换脚本（`_tools/convert_guoba_schema.py`）要读这份定义
去生成 `_conf_schema.json`，而那个脚本跑在容器外，不能 import `astrbot.api`。
所以把数据独立出来，两边共用同一份，避免"改了代码忘了改 schema"的漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CookieSpec:
    """一个平台的 Cookie 拆解方案。"""

    platform: str
    """平台 key，和 main.py 的 _COOKIE_FIELDS 对应。"""

    group: str
    """配置分组（对应 _conf_schema.json 的顶层 key）。"""

    raw_field: str
    """整段 Cookie 字段名（用户直接粘贴用）。"""

    keys: list[str]
    """逐个填写的 Cookie key，顺序即拼接顺序。"""

    label: str = ""
    """中文名，日志和 schema 描述用。"""

    raw_is_single: bool = False
    """raw_field 里存的是单值而不是完整串时，需要补上前缀。

    比如 B 站的 biliSessData 只存 SESSDATA 的值、小黑盒只存 token 的值。
    """

    raw_key: str = ""
    """``raw_is_single`` 为真时，单值对应的 key 名。"""

    notes: dict[str, str] = field(default_factory=dict)
    """每个 key 的填写说明；``__required__`` 是特殊键，用 ``|`` 分隔必需项。"""

    @property
    def fields_name(self) -> str:
        """逐项填写用的嵌套字段名。"""
        return f"{self.raw_field}Fields"

    @property
    def raw_path(self) -> str:
        return f"{self.group}.{self.raw_field}"

    @property
    def fields_path(self) -> str:
        return f"{self.group}.{self.fields_name}"

    @property
    def required_any(self) -> list[str]:
        """至少要填其中一个，否则这根 Cookie 等于没用。"""
        raw = self.notes.get("__required__", "")
        return raw.split("|") if raw else []

    def note_for(self, key: str) -> str:
        return self.notes.get(key, "")


# ==========================================================================
# 各平台的拆解方案
# ==========================================================================
#
# key 列表严格来自原插件 config/tools.yaml 与 constants 里的格式注释，
# 不是猜的。快手是本移植版新增（原版快手不需要 Cookie）。
#
COOKIE_SPECS: tuple[CookieSpec, ...] = (
    CookieSpec(
        platform="douyin",
        group="douyin",
        raw_field="douyinCookie",
        label="抖音",
        keys=[
            "odin_tt",
            "passport_fe_beating_status",
            "sid_guard",
            "uid_tt",
            "uid_tt_ss",
            "sid_tt",
            "sessionid",
            "sessionid_ss",
            "sid_ucp_v1",
            "ssid_ucp_v1",
            "passport_assist_user",
            "ttwid",
        ],
        notes={
            "sessionid": "登录态主凭据，最关键的一个",
            "ttwid": "设备指纹，有它成功率明显更高",
            "odin_tt": "设备标识",
            "sid_guard": "会话有效期凭据",
            "uid_tt": "用户标识",
            "__required__": "sessionid|ttwid",
        },
    ),
    CookieSpec(
        platform="bili",
        group="bili",
        raw_field="biliSessData",
        label="哔哩哔哩",
        keys=["SESSDATA", "bili_jct", "DedeUserID", "buvid3"],
        raw_is_single=True,
        raw_key="SESSDATA",
        notes={
            "SESSDATA": "必需。F12 -> Application -> Cookies 里的 SESSDATA",
            "bili_jct": "选填，CSRF 校验用",
            "DedeUserID": "选填，你的 UID",
            "buvid3": "选填，设备标识",
            "__required__": "SESSDATA",
        },
    ),
    CookieSpec(
        platform="kuaishou",
        group="other",
        raw_field="kuaishouCookie",
        label="快手",
        keys=["did", "didv", "kpn", "clientid"],
        notes={
            "did": "设备 ID，最关键的一个",
            "didv": "设备 ID 的校验值，一般和 did 成对出现",
            "kpn": "客户端标识，网页端一般是 KUAISHOU_VISION",
            "clientid": "客户端 ID",
            "__required__": "did|didv",
        },
    ),
    CookieSpec(
        platform="weibo",
        group="other",
        raw_field="weiboCookie",
        label="微博",
        keys=["_T_WM", "WEIBOCN_FROM", "MLOGIN", "XSRF-TOKEN", "M_WEIBOCN_PARAMS"],
        notes={
            "_T_WM": "登录态主凭据",
            "MLOGIN": "是否已登录标记（1 表示已登录）",
            "XSRF-TOKEN": "CSRF 令牌",
            "__required__": "_T_WM|MLOGIN",
        },
    ),
    CookieSpec(
        platform="xiaoheihe",
        group="xiaoheihe",
        raw_field="xiaoheiheCookie",
        label="小黑盒",
        keys=["x_xhh_tokenid"],
        raw_is_single=True,
        raw_key="x_xhh_tokenid",
        notes={
            "x_xhh_tokenid": "小黑盒的登录令牌，只有这一个字段",
            "__required__": "x_xhh_tokenid",
        },
    ),
)

SPEC_BY_PLATFORM: dict[str, CookieSpec] = {spec.platform: spec for spec in COOKIE_SPECS}


def get_spec(platform: str) -> CookieSpec | None:
    return SPEC_BY_PLATFORM.get(platform)


# ==========================================================================
# 必备字段体检
# ==========================================================================
#
# 用户在聊天里设置 Cookie（``#R配置 cookie <平台> <整串>``）时用这张表把关。
#
# **语义：允许不完整，但必须带上最关键的那几个。**
# 现实里没人能把 Cookie 复制得一个不漏，而且平台经常会加新的附属字段，
# 要求填全只会把人挡在门外。但「一个必备项都没有」的串一定是填错了 ——
# 常见的是粘错平台、或者只从 F12 里复制了某个不相干的字段 —— 这种直接
# 拒收，比写进配置让解析空转（现象是「配了 Cookie 还是拿不到内容」）好查得多。
#
# 值里是**任一命中即可**（``|`` 关系），不是全都要。没有登记的平台不做
# 体检，只要求「像 Cookie」（含至少一个 ``key=value``）。
COOKIE_REQUIRED_ANY: dict[str, tuple[str, ...]] = {
    "douyin": ("sessionid", "ttwid"),
    "bili": ("SESSDATA",),
    "kuaishou": ("did", "didv"),
    "weibo": ("_T_WM", "MLOGIN"),
    "xiaoheihe": ("x_xhh_tokenid",),
    # 网页端解析：a1 是设备标识（几乎总有），web_session 是登录态
    "xiaohongshu": ("web_session", "a1"),
    "miyoushe": ("cookie_token", "ltoken", "account_id"),
    # 视频号走腾讯元宝的接口，字段名上游没公开稳定约定，只认最核心的
    "weixinChannel": ("hy_user", "hy_token"),
    "netease": ("MUSIC_U",),
    "qqmusic": ("qqmusic_key", "qm_keyst"),
}


def parse_cookie_keys(raw: str) -> set[str]:
    """把 ``a=1; b=2`` 拆成字段名集合（保留原始大小写）。"""
    keys: set[str] = set()
    for part in str(raw or "").split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name = piece.split("=", 1)[0].strip()
        if name:
            keys.add(name)
    return keys


def check_cookie(platform: str, raw: str) -> tuple[bool, str]:
    """体检一段 Cookie，返回 ``(是否通过, 给用户看的说明)``。

    只拦两种情况：空的 / 完全不像 Cookie 的 / 必备字段一个都没有的。
    缺几个附属字段不拦 —— 测试过能用的 Cookie 经常就是这么来的。
    """
    text = str(raw or "").strip()
    if not text:
        return False, "内容是空的"

    keys = parse_cookie_keys(text)
    if not keys:
        return False, "看起来不像 Cookie（没找到 `key=value` 结构）"

    required = COOKIE_REQUIRED_ANY.get(platform)
    if not required:
        return True, ""

    lowered = {k.lower() for k in keys}
    if any(item.lower() in lowered for item in required):
        return True, ""

    need = " 或 ".join(required)
    return False, (
        f"缺少必备字段：至少要包含 **{need}** 里的一个（当前解析出 "
        f"{len(keys)} 个字段，一个都不是）"
    )
