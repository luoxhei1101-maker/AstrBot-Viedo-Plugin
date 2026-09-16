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
| 抖音 | 🟡 部分 | 走 **SSR 免 Cookie** 路线，拿不到评论 / 直播 / 部分高清档 |
| 哔哩哔哩 | 🟡 部分 | 免登录主干（信息 + 360P 直链）；WBI 签名 / BBDown / 番剧 / 直播 / 评论截图未移植 |
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
