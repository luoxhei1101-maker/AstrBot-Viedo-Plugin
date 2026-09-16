"""常量层：平台规则表 + 全部第三方接口地址。

对应原仓库的：
- ``constants/tools.js``   （接口地址）
- ``constants/constant.js``（通用常量）
- ``constants/resolve.js`` （平台开关名）
- ``apps/tools.js`` 的 rule 表（平台识别正则）

原版把「正则 -> 处理函数」写死在 Yunzai 的 rule 数组里，这里改成数据表，
由 ``platforms/registry.py`` 统一派发。新增平台只要往表里加一条 + 写一个 resolver。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ==========================================================================
# 一、第三方接口地址（原 constants/tools.js）
# ==========================================================================

# ---- 通用解析接口（多接口轮换，sign 越小越优先）----
GENERAL_REQ_LINKS: tuple[str, ...] = (
    "http://47.99.158.118/video-crack/v2/parse?content={}",
    "https://api.jkyai.top/API/jhspjx.php?url={}",
    "https://api.yujn.cn/api/pipixia.php?url={}",
    "https://api.bugpk.com/api/pipixia?url={}",
)

# ---- B 站 ----
BILI_VIDEO_INFO = "https://api.bilibili.com/x/web-interface/view"
BILI_BVID_TO_CID = "https://api.bilibili.com/x/player/pagelist?bvid={bvid}&jsonp=jsonp"
BILI_PLAY_STREAM = (
    "https://api.bilibili.com/x/player/wbi/playurl"
    "?cid={cid}&bvid={bvid}&qn={qn}&fnval={fnval}&fourk={fourk}"
)
BILI_BANGUMI_STREAM = (
    "https://api.bilibili.com/pgc/player/web/playurl"
    "?ep_id={ep_id}&cid={cid}&qn={qn}&fnval={fnval}&fourk={fourk}"
)
BILI_EP_INFO = "https://api.bilibili.com/pgc/view/web/season?ep_id={}"
BILI_SSID_INFO = "https://api.bilibili.com/pgc/web/season/section?season_id={}"
BILI_ARTICLE_INFO = "https://api.bilibili.com/x/article/viewinfo?id={}"
BILI_DYNAMIC = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail?id={}"
    "&features=onlyfansVote,onlyfansAssetsV2,decorationCard,htmlNewStyle,ugcDelete,"
    "editable,opusPrivateVisible,tribeeEdit,avatarAutoTheme,avatarTypeOpus"
)
BILI_SUMMARY = "https://api.bilibili.com/x/web-interface/view/conclusion/get"
BILI_ONLINE = "https://api.bilibili.com/x/player/online/total?bvid={0}&cid={1}"
BILI_REPLY_PAGE = "https://api.bilibili.com/x/v2/reply?type=1&oid={oid}&sort=1&ps={ps}&pn=1&nohot=0"
BILI_REPLY_WBI_MAIN = "https://api.bilibili.com/x/v2/reply/wbi/main"
BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
BILI_NAV_STAT = "https://api.bilibili.com/x/web-interface/nav/stat"
BILI_SCAN_CODE_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_SCAN_CODE_DETECT = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={}"
BILI_STREAM_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info"
BILI_STREAM_FLV = "https://api.live.bilibili.com/room/v1/Room/playUrl"

# ---- 抖音 ----
DY_INFO = (
    "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={}"
    "&pc_client_type=1&version_code=190500&version_name=19.5.0&cookie_enabled=true"
    "&screen_width=1344&screen_height=756&browser_language=zh-CN&browser_platform=Win32"
    "&browser_name=Firefox&browser_version=118.0&browser_online=true&engine_name=Gecko"
    "&engine_version=109.0&os_name=Windows&os_version=10&cpu_core_num=16&device_memory=&platform=PC"
)
DY_COMMENT = (
    "https://www.douyin.com/aweme/v1/web/comment/list/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={}"
    "&cursor=0&count=20&item_type=0&insert_ids=&whale_cut_token=&cut_version=1&rcFT="
    "&pc_client_type=1&version_code=170400&version_name=17.4.0&cookie_enabled=true"
    "&screen_width=1920&screen_height=1080&browser_language=zh-CN&browser_platform=Win32"
    "&browser_name=Chrome&browser_version=124.0.0.0&browser_online=true&engine_name=Blink"
    "&engine_version=124.0.0.0&os_name=Windows&os_version=10&cpu_core_num=20&device_memory=8"
    "&platform=PC&downlink=10&effective_type=4g&round_trip_time=50&webid=7361743797237679616"
)
DY_EMOJI_LIST = (
    "https://www.douyin.com/aweme/v1/web/emoji/list"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web&publish_video_strategy_type=2"
    "&need_all=true&update_version_code=170400&pc_client_type=1&version_code=170400"
    "&version_name=17.4.0&cookie_enabled=true&screen_width=1920&screen_height=1080"
    "&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=124.0.0.0"
    "&browser_online=true&engine_name=Blink&engine_version=124.0.0.0&os_name=Windows"
    "&os_version=10&device_memory=8&platform=PC&downlink=10&effective_type=4g"
    "&round_trip_time=50&webid=7361743797237679616"
)
DY_TOUTIAO_INFO = "https://aweme.snssdk.com/aweme/v1/play/?video_id={}&ratio=1080p&line=0"
DY_TTWID_REGISTER = "https://ttwid.bytedance.com/ttwid/union/register/"
DY_SHARE_VIDEO_PAGE = "https://www.iesdouyin.com/share/video/{}/"
DY_SHARE_NOTE_PAGE = "https://www.iesdouyin.com/share/note/{}/"
DY_LIVE_INFO = (
    "https://live.douyin.com/webcast/room/web/enter/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1"
    "&version_code=190500&version_name=19.5.0&cookie_enabled=true&screen_width=1920"
    "&screen_height=1080&browser_language=zh-CN&browser_platform=Win32&browser_name=Firefox"
    "&browser_version=124.0&browser_online=true&engine_name=Gecko&engine_version=122.0.0.0"
    "&os_name=Windows&os_version=10&cpu_core_num=12&device_memory=8&platform=PC"
    "&web_rid={}&room_id_str={}"
)
DY_LIVE_INFO_2 = (
    "https://webcast.amemv.com/webcast/room/reflow/info/"
    "?type_id=0&live_id=1&sec_user_id=&version_code=99.99.99&app_id=1128&room_id={}"
)

# ---- 小红书 ----
XHS_VIDEO = "http://sns-video-bd.xhscdn.com/"
XHS_REQ_LINK = "https://www.xiaohongshu.com/explore/"

# ---- X / Twitter ----
TWITTER_TWEET_INFO = "https://api.twitter.com/2/tweets?ids={}"

# ---- Instagram ----
IG_TEMP_PARSE_API = "https://downloader-api.bhwa233.com/api/parse?url={}"

# ---- 微博 ----
WEIBO_SINGLE_INFO = "https://m.weibo.cn/statuses/show?id={}"

# ---- 微视 ----
WEISHI_VIDEO_INFO = "https://h5.weishi.qq.com/webapp/json/weishi/WSH5GetPlayPage?feedid={}"

# ---- 米游社 ----
MIYOUSHE_ARTICLE = "https://bbs-api.miyoushe.com/post/wapi/getPostFull?post_id={}"

# ---- 小黑盒 ----
XHH_BBS_LINK = "https://api.xiaoheihe.cn/bbs/app/link/tree"
XHH_GAME_LINK = "https://api.xiaoheihe.cn/game/get_game_detail"
XHH_CONSOLE_LINK = "https://api.xiaoheihe.cn/game/console/get_game_detail"
XHH_MOBILE_LINK = "https://api.xiaoheihe.cn/game/mobile/get_game_detail"

# ---- 音乐 ----
NETEASE_SONG_DOWNLOAD = "https://neteasecloudmusicapi.vercel.app"
NETEASE_SONG_DETAIL = "https://neteasecloudmusicapi.vercel.app"
NETEASE_API_CN = "http://118.89.80.17:3000"
NETEASE_TEMP_API = "https://www.hhlqilongzhu.cn/api/dg_wyymusic.php?gm={}&n=1&type=json"
QQ_MUSIC_TEMP_API = "https://www.hhlqilongzhu.cn/api/dg_QQmusicflac.php?msg={}&n=1&type=json"
QISHUI_MUSIC_TEMP_API = "https://api.cenguigui.cn/api/qishui/?msg={}&limit=1&type=json&n=1"

# ---- 微信视频号 / 元宝 ----
WXCHANNEL_YUANBAO_PARSE = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
WXCHANNEL_FEED_INFO = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
YUANBAO_CHAT = "https://yuanbao.tencent.com/api/chat/"
YUANBAO_CONVERSATION_CREATE = "https://yuanbao.tencent.com/api/user/agent/conversation/create"
YUANBAO_CONVERSATION_CLEAR = "https://yuanbao.tencent.com/api/user/agent/conversation/v1/clear"
YUANBAO_CONVERSATION_UPDATE_MODEL = (
    "https://yuanbao.tencent.com/api/user/agent/conversation/updateModel"
)

# ---- 番剧搜索 / 其他 ----
ANIME_SERIES_SEARCH_LINK = "https://ylu.cc/so.php?wd="
ANIME_SERIES_SEARCH_LINK2 = "https://yhdm.one/search?q="
HIBI_API_SERVICE = "http://115.120.205.58:8080/api"

# 小黑盒签名盐值（原 utils/xiaoheihe.js）
XHH_SALT = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


# ==========================================================================
# 二、通用工具常量（原 constants/constant.js 的一部分）
# ==========================================================================

COMMON_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/70.0.3538.25 Mobile Safari/537.36"
)

# B 站画质档位：值越大越清晰，用于「智能分辨率」
BILI_RESOLUTION_LIST = {
    "4K": 120,
    "1080P60": 116,
    "1080P+": 112,
    "1080P": 80,
    "720P": 64,
    "480P": 32,
    "360P": 16,
}

# 抖音视频类型映射
DOUYIN_TYPE_MAP = {
    2: "image",
    4: "video",
    68: "image",
    150: "long_video",
}

# 翻译语种映射（原 transMap）
TRANS_MAP = {
    "zh": "中文",
    "en": "英语",
    "jp": "日语",
    "kr": "韩语",
    "fr": "法语",
    "de": "德语",
    "ru": "俄语",
    "es": "西班牙语",
}

# AI 总结的提示词（原 SUMMARY_PROMPT）
SUMMARY_PROMPT = (
    "请阅读以下网页内容并用中文总结，要求：\n"
    "1. 先用一句话概括核心结论\n"
    "2. 再分点列出关键信息（不超过 8 条）\n"
    "3. 不要编造原文没有的内容\n\n"
    "网页内容：\n{content}"
)

# 支持的 AI 总结站点（原 rule 里的 linkShareSummary 分支）
LINK_SUMMARY_HOSTS: tuple[str, ...] = (
    "mp.weixin.qq.com",
    "arxiv.org",
    "sspai.com",
    "chinadaily.com.cn",
    "zhihu.com",
    "github.com",
    "v2ex.com",
)


# ==========================================================================
# 三、通用解析接口（供 core/general_adapter.py 使用）
# ==========================================================================


@dataclass(frozen=True)
class ParseEndpoint:
    """一个第三方解析接口。"""

    sign: int
    template: str
    remark: str = ""

    def build(self, target_url: str) -> str:
        return self.template.replace("{}", target_url)


PARSE_ENDPOINTS: tuple[ParseEndpoint, ...] = (
    ParseEndpoint(1, GENERAL_REQ_LINKS[0], "返回 { data: { url, imageUrl, title } }"),
    ParseEndpoint(2, GENERAL_REQ_LINKS[1], "返回 { data: { url, images, title, author } }"),
    ParseEndpoint(3, GENERAL_REQ_LINKS[2], "返回 { data: { url, type } }"),
    ParseEndpoint(4, GENERAL_REQ_LINKS[3], "返回 { data: { url, imgurl } }，最慢，放最后"),
)

# 解析结果里可能出现的图片字段名
IMAGE_FIELD_NAMES: tuple[str, ...] = ("images", "imageUrl", "pics", "imgurl")

# 通用适配器覆盖的平台（platforms/general.py 用）
GENERAL_PLATFORM_KEYS: tuple[str, ...] = (
    "kuaishou",
    "ixigua",
    "pipixia",
    "pipigx",
    "qq_xsj",
    "tieba",
    "jike",
    "douyin_gif",
)


# ==========================================================================
# 四、平台识别规则表
# ==========================================================================


@dataclass
class PlatformRule:
    """一条自动识别规则。"""

    key: str
    """平台标识，配置里按这个开关。"""

    name: str
    """中文名，日志和回复文案用。"""

    pattern: str
    """识别正则。"""

    resolver: str
    """解析器注册名，对应 platforms/registry.py 里的键。"""

    enabled: bool = True
    extra: dict = field(default_factory=dict)


# 自动识别规则（对应原 apps/tools.js 的 rule 数组）
AUTO_RULES: tuple[PlatformRule, ...] = (
    PlatformRule(
        "douyin",
        "抖音",
        r"(?:v|live)\.douyin\.com|webcast\.amemv\.com|iesdouyin\.com|"
        r"www\.douyin\.com/(?:video|note|live|share|jingxuan|discover)",
        "general",
        extra={"prefer": "general"},  # 无 Cookie 时降级走通用适配器
    ),
    PlatformRule("tiktok", "TikTok", r"(?:www|vt|vm)\.tiktok\.com", "tiktok"),
    PlatformRule(
        "bili",
        "哔哩哔哩",
        r"(?:bilibili\.com|b23\.tv|bili2233\.cn|m\.bilibili\.com|t\.bilibili\.com|^BV[1-9a-zA-Z]{10}$)",
        "bilibili",
    ),
    PlatformRule(
        "twitter_x",
        "Twitter",
        r"https?://(?:x|r|twitter)\.com/[0-9\-a-zA-Z_]{1,20}/status/[0-9]+",
        "twitter",
    ),
    PlatformRule(
        "instagram",
        "Instagram",
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/",
        "instagram",
    ),
    PlatformRule("acfun", "AcFun", r"(?:acfun\.cn|^ac[0-9]{8}$)", "acfun"),
    PlatformRule("xhs", "小红书", r"(?:xhslink\.(?:com|cn)|xiaohongshu\.com)", "xiaohongshu"),
    PlatformRule("bodianMusic", "波点音乐", r"h5app\.kuwo\.cn", "bodian"),
    PlatformRule(
        "kuaishou",
        "快手",
        r"(?:kuaishou\.com|chenzhongtech\.com)",
        "kuaishou",
        extra={"cookie_field": "kuaishouCookie"},
    ),
    PlatformRule(
        "general",
        "通用",
        r"(?:ixigua\.com|h5\.pipix\.com|h5\.pipigx\.com|s\.xsj\.qq\.com|"
        r"m\.okjike\.com|tieba\.baidu\.com)",
        "general",
    ),
    PlatformRule("sy2b", "YouTube", r"(?:youtube\.com|youtu\.be|music\.youtube\.com)", "youtube"),
    PlatformRule("miyoushe", "米游社", r"miyoushe\.com", "miyoushe"),
    PlatformRule("netease", "网易云音乐", r"(?:music\.163\.com|163cn\.tv)", "netease"),
    PlatformRule("weibo", "微博", r"(?:weibo\.com|m\.weibo\.cn)", "weibo"),
    PlatformRule("weishi", "微视", r"weishi\.qq\.com", "weishi"),
    PlatformRule("zuiyou", "最右", r"share\.xiaochuankeji\.cn", "zuiyou"),
    PlatformRule("freyr", "AM+Spotify", r"(?:music\.apple\.com|open\.spotify\.com)", "freyr"),
    PlatformRule(
        "linkShareSummary",
        "AI总结",
        r"(?:^#总结一下\s*https?://|"
        + "|".join(h.replace(".", r"\.") for h in LINK_SUMMARY_HOSTS)
        + r")",
        "link_summary",
    ),
    PlatformRule("qqMusic", "QQ音乐", r"y\.qq\.com", "qqmusic"),
    PlatformRule(
        "kugouMusic",
        "酷狗音乐",
        r"(?:t1\.kugou\.com|m\.kugou\.com/share/song\.html|www\.kugou\.com/share/|h5\.kugou\.com/v2/)",
        "kugou",
    ),
    PlatformRule("qishuiMusic", "汽水音乐", r"qishui\.douyin\.com", "qishui"),
    PlatformRule(
        "aircraft",
        "小飞机",
        r"https://t\.me/(?:c/\d+/\d+/\d+|c/\d+/\d+|\w+/\d+/\d+|\w+/\d+\?\w+=\d+|\w+/\d+)",
        "aircraft",
    ),
    PlatformRule("xiaoheihe", "小黑盒", r"xiaoheihe\.cn", "xiaoheihe"),
    PlatformRule("weixinChannel", "视频号", r"weixin\.qq\.com/sph/", "weixin_channel"),
)

# 命令式规则：需要显式触发，部分要管理员权限
COMMAND_RULES: tuple[dict, ...] = (
    {
        "key": "trans",
        "name": "翻译",
        "pattern": r"^(?:翻|trans)[" + "".join(TRANS_MAP) + r"]",
        "handler": "trans",
        "admin": False,
    },
    {"key": "biliScan", "name": "B站扫码", "pattern": r"^#(?:RBQ|rbq)$", "handler": "bili_scan", "admin": True},
    {"key": "biliState", "name": "B站状态", "pattern": r"^#(?:RBS|rbs)$", "handler": "bili_state", "admin": True},
    {
        "key": "neteaseStatus",
        "name": "网易云状态",
        "pattern": r"^#(?:网易云状态|rns|RNS|网易云云盘状态|rncs|RNCS)$",
        "handler": "netease_status",
        "admin": True,
    },
    {"key": "neteaseScan", "name": "网易云扫码", "pattern": r"^#(?:rnq|RNQ|rncq|RNCQ)$", "handler": "netease_scan", "admin": True},
    {"key": "kugouStatus", "name": "酷狗状态", "pattern": r"^#(?:酷狗状态|rks|RKS)$", "handler": "kugou_status", "admin": True},
    {"key": "kugouScan", "name": "酷狗扫码", "pattern": r"^#(?:rkq|RKQ)$", "handler": "kugou_scan", "admin": True},
)


# ==========================================================================
# 五、辅助函数
# ==========================================================================

# 抽链接的正则
URL_PATTERN = r"https?://[^\s\u4e00-\u9fff\"'<>]+"


def build_combined_pattern(rules: tuple[PlatformRule, ...] | list[PlatformRule]) -> str:
    """把所有规则拼成一条大正则，交给 AstrBot 的 filter.regex 做粗筛。"""
    return "|".join(f"(?:{r.pattern})" for r in rules)


def extract_urls(text: str) -> list[str]:
    """从文本里抽出所有 http(s) 链接。"""
    return re.findall(URL_PATTERN, text, re.IGNORECASE)


def match_rule(
    text: str, rules: tuple[PlatformRule, ...] | list[PlatformRule]
) -> PlatformRule | None:
    """反查命中的平台规则。"""
    for rule in rules:
        if re.search(rule.pattern, text, re.IGNORECASE | re.MULTILINE):
            return rule
    return None


def resolve_endpoints(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    """清洗用户配置的接口模板。"""
    if not raw:
        return list(GENERAL_REQ_LINKS)
    out = [t for t in raw if isinstance(t, str) and "{}" in t]
    return out or list(GENERAL_REQ_LINKS)
