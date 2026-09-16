# astrbot_plugin_rconsole

Yunzai-Bot 插件 [rconsole-plugin](https://gitee.com/kyrzy0416/rconsole-plugin)（作者 zhiyu1998）在 **AstrBot** 上的移植版。

群里发一条分享链接，机器人自动把视频 / 图片 / 音乐扒出来发回来。

## 安装

把整个目录放进 AstrBot 的插件目录，再到 WebUI 插件页点重载：

```bash
cd AstrBot/data/plugins
git clone https://gitee.com/MuLinYa259/astrbot-viedo.git astrbot_plugin_rconsole
```

本插件不依赖 AstrBot 之外的任何第三方库，不需要 pip 装东西。

## 使用

| 方式 | 示例 | 说明 |
|---|---|---|
| **自动** | 直接发 `https://v.douyin.com/xxx` | 群里**不需要 @ 机器人** |
| 命令 | `/解析 <链接>` | 自动识别没覆盖到的场景 |
| AI 总结 | `#总结一下 https://mp.weixin.qq.com/...` | 抓正文丢给 AstrBot 已配的 LLM |
| 翻译 | `翻en hello world` | 日/英/韩/法/德/俄/西，走 LLM |

自动识别之所以能免 @，是因为 AstrBot 的正则过滤器**不受 `wake_prefix` 约束**
（见 `astrbot/core/star/filter/regex.py` 注释）。这一点和原版 Yunzai 的 rule 行为一致。

## 配置面板：原 Guoba 面板已完整迁移

原插件用 [Guoba-Plugin](https://gitee.com/guoba-yunzai/guoba-plugin) 做可视化配置面板
（`guoba.support.js`，1006 行，81 个配置项）。**这些配置项已全部转换到 AstrBot 的插件配置页。**

转换不是手工抄的——用 Node 把 `supportGuoba()` 的返回值 dump 成 JSON，
再用脚本转换成 AstrBot 的 `_conf_schema.json`，所以字段名、提示文案、
下拉选项、分组结构都跟原面板一致：

| 分组 | 项数 | 内容 |
|---|---|---|
| `plugin` | 13 | 移植版自己的开关（总开关、启用平台、解析接口、发送方式…） |
| `global` | 13 | 黑名单、转发阈值、代理、视频编码、识别前缀 |
| `bili` | 22 | SESSDATA、画质、下载方式、显示项、评论 |
| `douyin` | 10 | Cookie、时长、压缩、评论、背景音乐 |
| `youtube` | 4 | 画质、时长、Cookie 路径 |
| `netease` | 9 | Cookie、音质、点歌 |
| `other` | 13 | 微博 / 小红书 / 视频号 / 快手 / 酷狗 / QQ音乐 / 链接总结 |
| `xiaoheihe` | 1 | 小黑盒 Cookie |
| `ai` | 3 | 原版识图接口（本移植版用 AstrBot 的 Provider，此项保留兼容） |
| `advanced` | 3 | 并发、清理计划 |

**合计 91 项**。打开 WebUI 的插件配置页即可，字段名沿用原面板（`biliSessData`、
`douyinCookie`…），所以**原 Yunzai 的配置可以照着搬过来**。

只有 5 项没搬——`pluginHome` / `helpDoc` / `tgChannel` / `orangeSideBar` /
`complementarySet`，这些在原面板里是纯展示的文档链接，不是可配置项。

> 转换脚本放在仓库外的 `E:\AstrBot-Pulgin\_tools\`：
> - `dump-guoba.mjs` —— 用 Node 调 `supportGuoba()` 把 schema 导成 `guoba-schema.json`
> - `convert_guoba_schema.py` —— 把 Guoba schema + `config/tools.yaml` 的默认值
>   转成 AstrBot 的 `_conf_schema.json`
>
> 原插件升级后重跑这两步即可同步新的配置项，不用手抄。

## Cookie 怎么填

**不用自己拼格式了。** 原插件要求用户手动拼成
`odin_tt=xxx;passport_fe_beating_status=xxx;sid_guard=xxx;...`，漏个分号就解析失败，
而且报错还看不出来是哪儿错了。现在每个平台都有**两种填法，二选一**：

| 填法 | 适合 | 怎么做 |
|---|---|---|
| ① **整段 Cookie** | 已经复制了一整段 | 直接粘进原字段（`抖音的Cookie` 等），插件原样透传 |
| ② **逐项填写** | 只想填关键的几个 | 点「添加条目」→ **下拉栏选字段名** → **输入框填值** |

逐项填写用的是 AstrBot 的 `dict` + `template_schema` 键值编辑器
（就是系统设置里「自定义请求体参数」那种控件）：可选字段名以下拉形式给出，
已选过的键会自动从下拉里移除，条目可增删。

插件按 `key=value; key=value; ...` 组装，**并按字段的定义顺序排列**
（有些服务端认顺序）。还会**做必需项体检**——缺关键项时在日志里点名，
不用你去猜为什么解析失败。

### 支持逐项填写的平台

| 平台 | 配置路径 | 可逐项填的 key | 至少填一个 |
|---|---|---|---|
| B站 | `bili` → 哔哩哔哩SESSDATA / **逐项填写** | `SESSDATA` `bili_jct` `DedeUserID` `buvid3` | `SESSDATA` |
| 抖音 | `douyin` → 抖音的Cookie / **逐项填写** | 原版列出的全部 12 个（`sessionid` `ttwid` `odin_tt` `sid_guard` `uid_tt` …） | `sessionid` 或 `ttwid` |
| 快手 | `other` → 快手的Cookie / **逐项填写** | `did` `didv` `kpn` `clientid` | `did` 或 `didv` |
| 微博 | `other` → 微博的Cookie / **逐项填写** | `_T_WM` `WEIBOCN_FROM` `MLOGIN` `XSRF-TOKEN` `M_WEIBOCN_PARAMS` | `_T_WM` 或 `MLOGIN` |
| 酷狗 | `other` → 酷狗的Cookie / **逐项填写** | `token` `userid` `dfid` | `token` 或 `userid` |
| 小黑盒 | `xiaoheihe` → 小黑盒的Cookie / **逐项填写** | `x_xhh_tokenid` | `x_xhh_tokenid` |

每个字段在下拉里都带一句说明（比如 `sessionid` 标了「登录态主凭据，最关键的一个」），
不用去翻文档猜哪个重要。

> key 列表不是我猜的，是原插件 `config/tools.yaml` 注释里明确写出的格式拆出来的。
> B 站和小黑盒的「整段」字段存的是单值，填进去会自动补 `SESSDATA=` / `x_xhh_tokenid=` 前缀。
> 下拉之外的字段也能加（`dict` 是自由映射），插件照样带上，不会静默丢弃。

### 配了 Cookie 之后有什么变化

**B站（提升最大）**

| | 未配 Cookie | 配了 Cookie |
|---|---|---|
| 接口 | `platform=html5` 老接口 | WBI 签名 + `x/player/wbi/playurl` |
| 画质 | 360P | 按 `bili` → 最高分辨率 配置（默认 1080P） |
| 格式 | 单个 mp4，自带音轨 | DASH 音视频分离 |
| 合并 | 不需要 | **用容器内的 ffmpeg 无损合并**（`-c copy`） |
| 编码 | AVC | 按 `global` → 视频编码选择 挑（HEVC > AV1 > AVC） |

WBI 签名算法在 `core/bili_wbi.py` 里完整实现（含 `mixin_key` 重排表与按日缓存），
不依赖任何第三方库。

**抖音** —— SSR 分享页带上 Cookie 后拿到的数据更完整，部分需要登录态的作品也只有带
Cookie 才可见。注意：抖音 web API 还需要 `a-bogus` 签名，这部分没有移植，
所以主路仍走 SSR。可在 `douyin` → **是否开启 SSR 兜底** 里控制降级行为。

**快手** —— 现在是**双路**：优先抓网页 SSR 页面抠 `__APOLLO_STATE__` 直解，
失败才回落第三方接口。配了 Cookie 成功率更高。这样快手不再单点依赖第三方接口——
你也看到了，默认 4 个接口里只有 1 个还活着。



## 平台支持状态

`✅ 完整` = 核心流程已移植并验证｜`🟡 部分` = 主干可用，高级功能未搬｜`⬜ 未移植` = 识别正常但解析会明确提示缺什么

| 平台 | 状态 | 说明 |
|---|---|---|
| 快手 / 西瓜 / 皮皮虾 / 皮皮搞笑 / QQ小世界 / 贴吧 / 即刻 | ✅ 完整 | 第三方接口轮换 |
| 微博 | ✅ 完整 | 正文 / 图集 / 视频，含 `mid2id` 的 base62 转换 |
| AcFun | ✅ 完整 | ajaxpipe 页面抠 JSON → ksPlay 取 m3u8 |
| 小黑盒 | 🟡 部分 | 帖子解析已移植（含原版的 hkey 签名算法）；游戏详情页未移植 |
| 米游社 | 🟡 部分 | 文章图文已移植 |
| 微视 | 🟡 部分 | 走官方接口，字段做了多路容错 |
| 抖音 | 🟡 部分 | 走 **SSR 免 Cookie** 路线，配 Cookie 后数据更完整；`a-bogus` 签名未移植 |
| 哔哩哔哩 | ✅ 主干完整 | **WBI 签名 + Cookie + DASH + ffmpeg 合并**全通（配 SESSDATA 可 1080P）；BBDown / 番剧 / 直播 / 评论截图未移植 |
| 快手 | ✅ 主干完整 | **网页 SSR 直解**（配 Cookie 更稳）+ 第三方接口兜底，双路 |
| 网易云 / QQ音乐 / 汽水 | 🟡 部分 | 第三方直链接口；扫码登录、歌单、音质选择未移植 |
| 酷狗 | 🟡 部分 | 需要你自建 `kugouApiServer`，配了才可用 |
| AI 链接总结 / 翻译 | ✅ 完整 | 改用 AstrBot 自带的 LLM，不用再单独填 API Key |
| 小红书 | ⬜ 未移植 | 需要 `xsec_token` 处理 + Cookie |
| TikTok | ⬜ 未移植 | 需要 cycletls TLS 指纹伪装 |
| X / Instagram | ⬜ 未移植 | 需要 Cookie + 指纹库 |
| YouTube | ⬜ 未移植 | 需要 yt-dlp（检测到会提示） |
| 视频号 | ⬜ 未移植 | 需要腾讯元宝 Cookie |
| 波点 / 最右 / 小飞机 | ⬜ 未移植 | 私有接口待核对 |
| AppleMusic / Spotify | ⬜ 未移植 | 原版走外部 freyr 服务 |
| 各平台扫码登录 | ⬜ 未移植 | `#RBQ` / `#RNQ` / `#RKQ` 系列 |

> 未移植的平台**不是静默失败**：发链接会收到一条明确的「尚未移植 + 卡在哪 + 需要什么」。

## 这个移植做了什么、改了什么

### 架构

原版把 5862 行逻辑堆在 `apps/tools.js` 里。移植后分了层：

```
filter.regex 命中
      ↓
main.py  _dispatch()        平台识别 + 配置过滤 + 消息渲染
      ↓
platforms/base.py  注册表    按名字取 resolver
      ↓
platforms/*.py              只做「链接 -> 媒体地址」，不碰框架
      ↓
core/*.py                   HTTP / 外部命令 / 下载 等基础设施
```

resolver 不依赖 AstrBot，可以单独跑测试——这也是部署前能验证逻辑正确性的原因。

### API 对照

| Yunzai | AstrBot |
|---|---|
| `rule: [{reg, fnc}]` | `@filter.regex(合并正则)` |
| `e.reply(x)` | `yield event.plain_result(x)` |
| `segment.image(url)` | `Comp.Image.fromURL(url)` |
| `segment.video(path)` | `Comp.Video.fromFileSystem(path)` |
| `segment.record(path)` | `Comp.Record.fromURL(url)` |
| `Bot.makeForwardMsg()` | 无对应，改用多条结果 |
| `puppeteer.screenshot()` | `Star.html_render()`（模板需重做） |
| `permission: 'master'` | `event.is_admin()` |
| `config/*.yaml` + chokidar | `_conf_schema.json` + WebUI 表单 |
| 全局 `redis` | `Star.get_kv_data()` / `put_kv_data()` |
| 自建 OpenAI 调用 | `Context.get_using_provider_async()` |

### 相比原版的改动

1. **LLM 相关能力改为复用 AstrBot 的模型配置**。原版要用户自己填 `aiBaseURL` / `aiApiKey` / `aiModel`，现在直接用机器人已配好的 Provider。
2. **API 状态自检**。启动时探测 ffmpeg / yt-dlp / BBDown / aria2c / tdl，日志里直接列缺什么。
3. **死接口快速失败**。第三方解析接口挂掉最常见的形式是域名过期，代码对 DNS 失败直接快速失败，不在死域名上做无意义重试。
4. **临时文件交给框架回收**。用 `event.track_temporary_local_file()`，不用自己写清理任务。
5. **未移植平台显式报错**，而不是静默无响应。

## ⚠️ 关于第三方接口的存活状态

解析能力依赖第三方接口，而**这类接口的生命周期普遍很短**。部署时实测默认 4 个接口：

| 接口 | 实测结果 |
|---|---|
| #1 `47.99.158.118/video-crack/v2/parse` | ✅ **正常** |
| #2 `api.jkyai.top` | ❌ 域名已无法解析 |
| #3 `api.yujn.cn` | ⚠️ 返回 `code:404` |
| #4 `api.bugpk.com` | ⚠️ 返回 `code:400` |

**4 个里只有 1 个活着。** 好在原版的「多接口轮换」设计正好兜住了底。
接口全挂了就去 `parse_endpoints` 配置里换新的，不用改代码。

## 许可与致谢

本项目是 [rconsole-plugin](https://gitee.com/kyrzy0416/rconsole-plugin) 的移植版本，
业务逻辑与设计思路来自原项目，遵循原项目的开源许可协议 —— **木兰宽松许可证，第 2 版（Mulan PSL v2）**，
`LICENSE` 文件与原项目保持一致。

原项目 README 中的声明同样适用：素材来源于网络，仅供交流学习使用，严禁用于任何商业用途和非法行为。
