# astrbot_plugin_rconsole

> **本插件是 Yunzai-Bot 插件「R插件」（[rconsole-plugin](https://gitee.com/kyrzy0416/rconsole-plugin)，作者 zhiyu1998）在 AstrBot 上的移植版。**
>
> 业务逻辑与设计思路来自原项目，**同样遵循原项目的开源许可协议 —— 木兰宽松许可证第 2 版（Mulan PSL v2）**，`LICENSE` 文件与原项目保持一致。
> 原项目 README 的声明同样适用：素材来源于网络，仅供交流学习使用，严禁用于任何商业用途和非法行为。

群里发一条分享链接（抖音 / B站 / 快手 / 微博…），机器人自动把视频、图集、音乐扒出来发回来。

---

## 目录

- [必要环境](#必要环境)
- [安装步骤](#安装步骤)
- [推荐部署环境](#推荐部署环境)
- [怎么用：所有触发方式](#怎么用所有触发方式)
- [配置说明](#配置说明)
- [Cookie 怎么填](#cookie-怎么填)
- [支持的平台](#支持的平台)
- [常见问题](#常见问题)
- [架构与改动](#架构与改动)
- [许可与致谢](#许可与致谢)

---

## 必要环境

### AstrBot 本体

- AstrBot `>=4.16, <5`（本插件在 v4.28.1 上验证通过）
- Python 3.10+（AstrBot 自带，无需单独安装）

### 外部工具（按需）

| 工具 | 是否必需 | 用途 |
|---|---|---|
| `ffmpeg` + `ffprobe` | ✅ 必需（B站高清） | B站 DASH 音视频分离后合并 / 转码 / m3u8 拼接 |
| `yt-dlp` | 可选 | YouTube 等站点下载 |
| `BBDown` | 可选 | B站高画质下载（原版可选） |
| `aria2c` | 可选 | 多线程下载器 |
| `tdl` | 可选 | Telegram 文件下载 |

> 插件启动时会自动探测这些工具，缺哪个日志里会直接列出来。
> 除了 ffmpeg，其余都是「用不到就不装」，不影响主要功能。

### Python 库（仅扫码登录）

B站扫码登录（`#RBQ`）需要 `qrcode[pil]`。**AstrBot 官方 Docker 镜像已内置**；
本地部署的话执行 `pip install qrcode[pil]` 即可。

> 插件本身**不依赖 AstrBot 之外的任何第三方库**（HTTP 用 AstrBot 自带的 aiohttp）。

---

## 安装步骤

### 方式一：Docker 部署（推荐）

AstrBot 官方推荐用 Docker 跑，插件目录是宿主机上挂载出来的 `AstrBot/data/plugins`：

```bash
# 1. 先按 AstrBot 官方文档起容器（把 data 目录挂出来）
docker run -d --name astrbot \
  -p 6185:6185 \
  -v $PWD/AstrBot/data:/AstrBot/data \
  soulter/astrbot

# 2. 把插件 clone 到插件目录
cd AstrBot/data/plugins
git clone https://gitee.com/MuLinYa259/astrbot-viedo.git astrbot_plugin_rconsole

# 3. 重启容器（或到 WebUI 点重载插件）
docker restart astrbot
```

### 方式二：本地部署

AstrBot 本地（非 Docker）跑的话，插件目录在 AstrBot 安装目录下的 `data/plugins`：

```bash
cd <AstrBot安装目录>/data/plugins
git clone https://gitee.com/MuLinYa259/astrbot-viedo.git astrbot_plugin_rconsole
```

### 方式三：下载 zip（不装 git）

从仓库页下载 zip → 解压 → 把**解压出来的目录**放进 `data/plugins`，目录名保持
`astrbot_plugin_rconsole`（去掉 zip 自带的 `-main` 后缀）。

### 安装后

1. 到 AstrBot WebUI 的「插件」页点**重载**（或重启 AstrBot）。
2. 打开插件配置页，按需填 Cookie、开关平台。
3. 看启动日志：会打印一行「识别规则 X 条 / resolver X 个」+ 外部工具探测结果，
   缺 ffmpeg 之类会直接提示。

---

## 推荐部署环境

| 项 | 推荐 | 说明 |
|---|---|---|
| 系统 | **Linux（Debian / Ubuntu）+ Docker** | AstrBot 官方推荐；容器化隔离、一键升级、免手工配依赖 |
| 内存 | **≥ 2GB**（下载合并高清建议 4GB+） | AstrBot 本体 ~260MB + 插件；下载 / 合并大视频会临时占用 |
| 磁盘 | ≥ 5GB 空闲 | 视频 / 图集临时文件 + 本地缓存 |
| GPU | 不需要 | 纯网络 + ffmpeg，无推理负载 |

> Windows / macOS 也能跑（AstrBot 支持本地部署），但生产长期挂机更推荐 Linux。
> 特别提醒：走第三方云服务器时，记得在**安全组**放行 AstrBot 管理端口（默认 6185）
> 和消息平台（OneBot 等）的端口——云安全组拦截是「端口连不通」最常见的原因。

### 支持的消息平台

aiocqhttp（QQ OneBot）、Telegram、Discord、飞书 / Lark、企业微信、QQ 官方、Slack、钉钉。

---

## 怎么用：所有触发方式

### 一、自动识别（免 @）

群里**直接发链接**，机器人自动解析。**不需要 @ 机器人**。

| 平台示例 | 链接 |
|---|---|
| 抖音 | `https://v.douyin.com/xxxx/` |
| B站 | `https://b23.tv/xxxx`、`https://www.bilibili.com/video/BVxxxx`、纯 `BVxxxx` |
| B站小程序卡片 | 从 B站 App 分享到 QQ 的小程序卡片 |
| 快手 / 微博 / 小红书 / AcFun / 贴吧… | 对应的分享链接 |

> 自动识别之所以能免 @，是因为 AstrBot 的正则过滤器**不受 `wake_prefix` 约束**（见 `astrbot/core/star/filter/regex.py`），和原版 Yunzai 的 rule 行为一致。

### 二、命令式指令

| 指令 | 作用 | 权限 |
|---|---|---|
| `翻en <内容>` / `翻ja <内容>` / `翻kr <内容>` … | 翻译成指定语言（日/英/韩/法/德/俄/西） | 所有人 |
| `#总结一下 <链接>` | 抓网页正文，丢给 AstrBot 已配的 LLM 总结 | 所有人 |
| `#RBQ` / `#rbq` | **B站扫码登录**，扫码后 Cookie 自动写进配置 | 🔒 管理员 |
| `#RBS` / `#rbs` | 查当前 B站 Cookie 是否有效、是哪个号 | 🔒 管理员 |
| `#网易云状态` / `#rns` | 查网易云 Cookie 状态 | 🔒 管理员 |
| `#酷狗状态` / `#rks` | 查酷狗 Cookie 状态 | 🔒 管理员 |

> 🔒 标记的指令**只有 AstrBot 管理员能用**（名单在全局配置 `admins_id` 里）。普通成员触发时默认**静默忽略**（不回消息、避免暴露指令存在），只记一条告警日志。
> 带 🔒 但标记为「未移植」的指令（网易云 / 酷狗扫码 `#rnq` / `#rkq`）触发后会有明确的「尚未移植」提示，不会静默失败。

### 三、B站扫码登录（`#RBQ`）详解

1. 管理员在群里发 `#RBQ`
2. 机器人回一张二维码（容器内 `qrcode` + `PIL` 本地生成，不依赖外部服务）
3. 用 **B站手机客户端** 扫码 → 手机上点「确认登录」
4. `SESSDATA` / `bili_jct` / `DedeUserID` / `buvid3` **自动写进配置**，并回一条账号信息（昵称 / UID / 等级 / 会员状态）确认

二维码约 3 分钟过期，超时可在配置里调（`plugin` → B站扫码等待超时，默认 180 秒）。

---

## 配置说明

打开 AstrBot WebUI 的「插件配置」页。**原版的 Guoba 可视化面板已完整迁移过来**（81 个配置项全部转换），字段名沿用原面板，所以**原 Yunzai 的配置可以直接照搬**。

### 分组一览

| 分组 | 内容 |
|---|---|
| `plugin` | 移植版自己的开关：总开关、启用平台、发送方式、图集合并转发、下载并发… |
| `global` | 全局黑名单、转发阈值、代理、视频编码、识别前缀 |
| `bili` | SESSDATA、画质、下载方式、显示项、评论 |
| `douyin` | Cookie、时长、压缩、评论、背景音乐 |
| `youtube` | 画质、时长、Cookie 路径 |
| `netease` | Cookie、音质、点歌 |
| `other` | 微博 / 小红书 / 视频号 / 快手 / 酷狗 / QQ音乐 / 链接总结 |
| `xiaoheihe` | 小黑盒 Cookie |
| `ai` | 原版识图接口（本移植版复用 AstrBot 的 Provider，此项保留兼容） |
| `advanced` | 并发、清理计划 |

### 关键配置项速览

| 配置 | 默认 | 说明 |
|---|---|---|
| `plugin.enable` | 开 | 总开关 |
| `plugin.enabled_platforms` | 抖音/B站/通用/微博… | 只对勾选的平台生效 |
| `plugin.send_mode` | 直发链接 | `url`=直接发地址（快）；`download`=先下本地再发（稳） |
| `plugin.max_images` | 9 | 图集超过这个数就合并成「聊天记录」完整发送 |
| `plugin.download_concurrency` | 8 | 媒体并发下载数 |
| `plugin.album_forward_when_exceed` | 开 | 图集超限是否用合并转发 |

---

## Cookie 怎么填

**不用自己拼格式了。** 原插件要求手动拼 `odin_tt=xxx;sessionid=xxx;...`，漏个分号就失败。现在每个平台两种填法，二选一：

| 填法 | 适合 | 怎么做 |
|---|---|---|
| ① 整段 Cookie | 已复制一整段 | 直接粘进原字段（`抖音的Cookie` 等），原样透传 |
| ② 逐项填写 | 只想填关键几个 | 点「添加条目」→ 下拉选字段名 → 输入框填值（行内表格） |

逐项填写用的是 AstrBot 的 `template_list` 控件，渲染成**行内表格**——一行一个「字段名下拉 + 值输入框」。插件按 `key=value; key=value` 组装，还会做**必需项体检**（缺关键项在日志里点名）。

### 支持逐项填写的平台

| 平台 | 至少填一个 | 备注 |
|---|---|---|
| B站 | `SESSDATA` | 整段字段只存单值，会自动补 `SESSDATA=` 前缀 |
| 抖音 | `sessionid` 或 `ttwid` | 原版列出的 12 个 key 全部可填 |
| 快手 | `did` 或 `didv` | 本移植版新增（原版快手不走 Cookie） |
| 微博 | `_T_WM` 或 `MLOGIN` | |
| 酷狗 | `token` 或 `userid` | |
| 小黑盒 | `x_xhh_tokenid` | 整段字段只存单值 |

> 取法：浏览器登录后 F12 → Network → 任意请求 → Request Headers → Cookie 整段复制。

### 配了 Cookie 之后的变化

**B站（提升最大）**：未配 Cookie 走 360P 老接口；配了走 **WBI 签名 + DASH 高清**（默认 1080P），音视频分离后用容器内 ffmpeg 无损合并（`-c copy`）。WBI 签名算法在 `core/bili_wbi.py` 完整实现，不依赖第三方库。

**抖音**：SSR 分享页带 Cookie 数据更完整；主路仍走 SSR（web API 的 `a-bogus` 签名未移植）。

**快手**：双路——优先网页 SSR 直解，失败回落第三方接口。

---

## 支持的平台

`✅ 完整` = 核心流程已移植并验证｜`🟡 部分` = 主干可用，高级功能未搬｜`⬜ 未移植` = 识别正常但会明确提示缺什么

| 平台 | 状态 | 说明 |
|---|---|---|
| 快手 / 西瓜 / 皮皮虾 / 皮皮搞笑 / QQ小世界 / 贴吧 / 即刻 | ✅ | 第三方接口轮换 |
| 微博 | ✅ | 正文 / 图集 / 视频，含 `mid2id` base62 转换 |
| AcFun | ✅ | ajaxpipe 抠 JSON → m3u8 |
| 哔哩哔哩 | ✅ 主干 | WBI 签名 + Cookie + DASH + ffmpeg 合并；扫码登录；BBDown / 番剧 / 直播 / 评论截图未移植 |
| AI 总结 / 翻译 | ✅ | 复用 AstrBot 自带的 LLM |
| 抖音 | 🟡 | SSR 路线（配 Cookie 更完整）；`a-bogus` 签名未移植 |
| 小黑盒 | 🟡 | 帖子解析（含 hkey 签名）；游戏页未移植 |
| 米游社 / 微视 | 🟡 | 主干已移植 |
| 网易云 / QQ音乐 / 汽水 | 🟡 | 第三方直链；扫码、歌单、音质未移植 |
| 酷狗 | 🟡 | 需自建 `kugouApiServer` |
| 小红书 | ⬜ | 需 `xsec_token` + Cookie |
| TikTok / X / Instagram | ⬜ | 需 TLS 指纹伪装 + Cookie |
| YouTube | ⬜ | 需 yt-dlp |
| 视频号 | ⬜ | 需腾讯元宝 Cookie |
| 波点 / 最右 / 小飞机 / AppleMusic / Spotify | ⬜ | 私有接口 / 外部服务 |

> 未移植的平台**不是静默失败**：发链接会收到明确的「尚未移植 + 卡在哪 + 需要什么」提示。

---

## 常见问题

**Q：发了链接没反应、也没日志？**
先查 AstrBot 全局配置 `cmd_config.json` 里的 `plugin_set` 是不是被改成了空数组 `[]`（正常应是 `["*"]`）。这个配置会让所有插件的消息处理被跳过，症状就是「完全静默」。

**Q：第三方解析接口挂了？**
这类接口生命周期很短。默认 4 个通用接口里实测只有 1 个还活着（`47.99.158.118/video-crack/v2/parse`）。接口全挂了就去 `plugin.parse_endpoints` 配置里换新的，不用改代码。代码对 DNS 失败做了快速失败，不会在死域名上白等重试。

**Q：重复发同一个链接会重新解析吗？**
不会。插件有**作品缓存**（2 小时），命中缓存直接重发，跳过网络解析。

**Q：群里的普通成员能发链接让机器人解析吗？**
能。自动识别是免 @ 的，任何成员发链接都会解析。只有带 🔒 的管理指令才限制管理员。

---

## 架构与改动

### 架构分层

原版把 5862 行逻辑堆在 `apps/tools.js` 里，移植后分了层：

```
filter.regex 命中
      ↓
main.py  _dispatch()        平台识别 + 配置过滤 + 消息渲染（含缓存）
      ↓
platforms/base.py  注册表    按名字取 resolver
      ↓
platforms/*.py              只做「链接 -> 媒体地址」，不碰框架
      ↓
core/*.py                   HTTP / 下载 / 合并 / 签名 等基础设施
```

resolver 不依赖 AstrBot，可以脱离框架单独测试。

### API 对照（Yunzai → AstrBot）

| Yunzai | AstrBot |
|---|---|
| `rule: [{reg, fnc}]` | `@filter.regex(合并正则)` |
| `e.reply(x)` | `yield event.plain_result(x)` |
| `segment.image(url)` | `Comp.Image.fromURL(url)` |
| `segment.video(path)` | `Comp.Video.fromFileSystem(path)` |
| `segment.record(path)` | `Comp.Record.fromURL(url)` |
| `Bot.makeForwardMsg()` | `Comp.Node` + `Comp.Nodes`（合并转发） |
| `puppeteer.screenshot()` | `Star.html_render()` |
| `permission: 'master'` | `event.is_admin()` |
| `config/*.yaml` | `_conf_schema.json` + WebUI 表单 |
| 全局 `redis` | `Star.get_kv_data()` / `put_kv_data()` |
| 自建 OpenAI 调用 | `Context.get_using_provider_async()` |

### 相比原版的主要改动

1. **LLM 复用 AstrBot 的模型**：不用再填 `aiBaseURL` / `aiApiKey` / `aiModel`。
2. **启动自检**：探测 ffmpeg / yt-dlp / BBDown / aria2c / tdl，日志直接列缺什么。
3. **作品缓存**：重复链接 2 小时缓存，命中直接重发。
4. **先发简介再发媒体**：标题 / 作者 / 类型先返回，B站 DASH 合并延迟到渲染阶段，简介不被下载阻塞。
5. **死接口快速失败** + **未移植平台显式报错** + **临时文件交给框架回收**。

---

## 许可与致谢

本项目是 [rconsole-plugin](https://gitee.com/kyrzy0416/rconsole-plugin) 的移植版本，业务逻辑与设计思路来自原项目，遵循原项目的开源许可协议 —— **木兰宽松许可证，第 2 版（Mulan PSL v2）**。

原项目 README 中的声明同样适用：素材来源于网络，仅供交流学习使用，严禁用于任何商业用途和非法行为。
