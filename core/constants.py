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
# 新版图集/动图分享页。**注意**：这个页面是客户端 SPA，HTML 里没有
# _ROUTER_DATA，SSR 路径完全拿不到数据，必须走主接口（DY_INFO + a-bogus）。
DY_SHARE_SLIDES_PAGE = "https://www.iesdouyin.com/share/slides/{}/"
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

# 抖音作品类型映射，照抄原版 constants/constant.js 的 douyinTypeMap。
# 决定这条作品该按视频发还是按图文发——**不能靠猜**：
# 图集的 video.play_addr 里装的其实是背景音乐，把它当视频发出去，
# 用户收到的是一条指向音乐文件的"视频"。
DY_TYPE_MAP: dict[int, str] = {
    0: "video",
    2: "image",
    4: "video",
    51: "video",
    55: "video",
    58: "video",
    61: "video",
    68: "image",
    109: "video",
    150: "image",
}

# 画质探测的档位顺序（从高到低），对应 DY_TOUTIAO_INFO 里的 ratio 参数
DY_PLAY_RATIOS: tuple[str, ...] = ("1080p", "720p", "540p", "360p")
DY_COMPRESSED_PLAY_RATIOS: tuple[str, ...] = ("720p", "540p", "360p")

# 抖音匿名 ttwid 注册用的固定载荷（原版 TTWID_REGISTER_PAYLOAD）
DY_TTWID_PAYLOAD: dict = {
    "aid": 1768,
    "union": True,
    "needFid": False,
    "region": "cn",
    "cbUrlProtocol": "https",
    "service": "www.ixigua.com",
    "migrate_info": {"ticket": "", "source": "node"},
}

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

# ---- 点歌搜索（网易云 / QQ音乐）----
#
# 接口来源：参考 TRSS-Yunzai 的 xiaofei-plugin（https://github.com/xfdown/xiaofei-plugin）
# 的 ``apps/点歌.js``，并修正了它已经过期的取数路径（见 core/music_search.py 注释）。
#
# 两个平台的登录态要求不同：
# - **网易云**：搜索 + 取直链完全匿名可用；配 ``MUSIC_U`` 才能拿 VIP 歌（fee=1）的
#   高音质直链。
# - **QQ音乐**：搜索匿名可用；**取直链必须有登录态 Cookie**，匿名调 ``CgiGetVkey``
#   恒返回 ``result=104003``（= 需要登录/VIP）。Cookie 必需字段见 music_search.py
#   的模块 docstring。musickey 只有 12 小时有效期。
#
# 有意**不实现**酷狗：它的搜索匿名可用，但老取直链接口
# （``wwwapi.kugou.com/play/songinfo``）现在恒返 ``err_code=30020``，
# 而可用替代（``m.kugou.com/api/v1/wechat/index``）返回的是音频流本体、
# 没有可分享的 CDN 直链，不适合「发链接」这种交付方式。
NETEASE_SEARCH_API = "http://music.163.com/api/cloudsearch/pc"
NETEASE_SONG_URL_API = "https://interface3.music.163.com/api/song/enhance/player/url/v1"
NETEASE_SONG_OUTER_URL = "http://music.163.com/song/media/outer/url?id={id}"
NETEASE_SONG_PAGE = "http://music.163.com/#/song?id={id}"

QQ_MUSIC_SEARCH_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_MUSIC_VKEY_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_MUSIC_SONG_PAGE = "https://y.qq.com/n/ryqq/songDetail/{mid}"

# 网易云搜索接口要求带「PC 客户端」伪装 Cookie 才稳定返回结果
NETEASE_SEARCH_COOKIE = "os=pc; appver=2.9.7;"
# 网易云取直链接口要求 Android 客户端伪装（原插件也是这么带的）
NETEASE_URL_COOKIE = "versioncode=8008070; os=android; channel=xiaomi; appver=8.8.70;"

# QQ 音乐接口的 comm 块模板，照搬 xiaofei-plugin 的
# ``music_cookies.qqmusic.body``。取直链时把登录凭据合并进 ``comm``。
# ``guid`` 在运行时按 uin 重算，这里给个占位。
QQ_MUSIC_BODY_TEMPLATE: dict = {
    "comm": {
        "_channelid": "19",
        "_os_version": "6.2.9200-2",
        "authst": "",
        "ct": "19",
        "cv": "1891",
        "guid": "00000000000000000000000000000000",
        "patch": "118",
        "psrf_access_token_expiresAt": 0,
        "psrf_qqaccess_token": "",
        "psrf_qqopenid": "",
        "psrf_qqunionid": "",
        "tmeAppID": "qqmusic",
        "tmeLoginType": 2,
        "uin": "0",
        "wid": "0",
    }
}

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

# 原插件 config 里 biliResolution 存的是**下拉索引**，不是 qn。
# 对应 constants/constant.js 的 BILI_RESOLUTION_LIST（value -> qn）。
# 面板上显示的中文名见 BILI_QUALITY_INDEX_LABELS。
BILI_QUALITY_INDEX_TO_QN: dict[int, int] = {
    0: 127,   # 8K 超高清
    1: 126,   # 杜比视界
    2: 125,   # HDR 真彩
    3: 120,   # 4K 超清
    4: 116,   # 1080P60 高帧率
    5: 112,   # 1080P+ 高码率
    6: 80,    # 1080P 高清
    7: 74,    # 720P60 高帧率
    8: 64,    # 720P 高清
    9: 32,    # 480P 清晰
    10: 16,   # 360P 流畅
}

BILI_QUALITY_INDEX_LABELS: tuple[str, ...] = (
    "8K 超高清",
    "杜比视界",
    "HDR 真彩",
    "4K 超清",
    "1080P60 高帧率",
    "1080P+ 高码率",
    "1080P 高清",
    "720P60 高帧率",
    "720P 高清",
    "480P 清晰",
    "360P 流畅",
)

# qn -> 展示名，用于日志和回复文案
BILI_QN_TO_NAME: dict[int, str] = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
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
        "douyin",
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
    # 点歌搜索。注意 pattern 里 `点歌` 后面必须跟内容（`(.+)`），否则单独一个
    # 「点歌」会命中并回一条空搜索；`(?:网易云|QQ|qq)?` 是可选平台前缀，
    # 不写就默认按「网易云 → QQ音乐」依次尝试。
    {
        "key": "musicSearch",
        "name": "点歌搜索",
        "pattern": r"^(?:#|/)?点歌\s*(?:网易云|网抑云|网易|QQ音乐|qq音乐|QQ|qq)?\s*(.+)$",
        "handler": "music_search",
        "admin": False,
    },
)


# ==========================================================================
# 五、辅助函数
# ==========================================================================

# 抽链接的正则
URL_PATTERN = r"https?://[^\s\u4e00-\u9fff\"'<>]+"

# 预编译缓存：pattern 串 -> re.Pattern
# `match_rule` 每条 URL 都要按顺序试最多 24 条规则，`re.search(pattern_str, ...)`
# 虽然走 Python 内部的 `re._cache`，但每次仍要做一次「拼 key + 查字典 + 判断是否
# 需要编译」的开销，而且那个缓存上限只有 512 条、可能被别的插件挤掉。
# 这里自己按 pattern 串缓存编译结果，命中后直接调 `Pattern.search`，最省。
_COMPILED: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    """按 pattern 串取编译好的正则，只编译一次。"""
    got = _COMPILED.get(pattern)
    if got is None:
        got = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        _COMPILED[pattern] = got
    return got


def build_combined_pattern(rules: tuple[PlatformRule, ...] | list[PlatformRule]) -> str:
    """把所有规则拼成一条大正则，交给 AstrBot 的 filter.regex 做粗筛。"""
    return "|".join(f"(?:{r.pattern})" for r in rules)


def extract_urls(text: str) -> list[str]:
    """从文本里抽出所有 http(s) 链接。"""
    return _compiled(URL_PATTERN).findall(text)


def match_rule(
    text: str, rules: tuple[PlatformRule, ...] | list[PlatformRule]
) -> PlatformRule | None:
    """反查命中的平台规则。"""
    for rule in rules:
        if _compiled(rule.pattern).search(text):
            return rule
    return None


def resolve_endpoints(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    """清洗用户配置的接口模板。"""
    if not raw:
        return list(GENERAL_REQ_LINKS)
    out = [t for t in raw if isinstance(t, str) and "{}" in t]
    return out or list(GENERAL_REQ_LINKS)
