# 更新日志

## v1.1.6（2026-09-17）

### 修复

- **B 站（及其它走 DASH 合并的）视频发不出来**，日志报
  `Failed to send the message chain ... ENOENT: no such file or directory,
  realpath '/tmp/astrbot_plugin_rconsole/merge/xxx.mp4'`。

  **根因：AstrBot 与协议端是两个容器、没有任何共享挂载。** 实测本项目的部署
  环境：

  - `astrbot` 容器只挂了 `/www/server/astrbot/data -> /AstrBot/data`
  - `snowluma`(NapCat) 容器挂的是自己的三个 volume
  - 两边**没有任何共享目录**（在 astrbot 里写 `/AstrBot/data/temp/x`，
    NapCat 里读不到）

  而 AstrBot 的 aiocqhttp 适配器对不同组件的处理**不一样**：

  | 组件 | 适配器行为 | 跨容器 |
  |---|---|---|
  | `Image` / `Record` | 转 `base64://` 再发 | ✅ |
  | `Video` | **原样传 `file:///path`** | ❌ |

  所以图片一直能发（走 base64），而视频把 `file:///tmp/.../xxx.mp4` 原样交给
  NapCat，NapCat 去 `realpath` 这个路径必然 ENOENT，**整条消息链失败**。

  **修复**：新增 `main._video_component()`，视频统一用
  `Comp.Video.fromBase64()` 构造（和图片的处理方式对齐），并先检查文件是否
  存在。所有发视频的地方（B 站合并产物、`local_videos`、`send_mode=download`、
  抖音图集动图的直发/限流/合并转发三档）全部改走它。

  代价是消息体膨胀约 33%（base64 编码开销），但这是**唯一能跨容器送达的
  方式**。`tests/test_album_send_path.py` 用 AST 断言锁死「真实代码里不得出现
  `Comp.Video.fromFileSystem`」。

- **快手链接完全不触发**（日志里连"跳过"都没有）。原因不是识别正则 —— 实测
  `v.kuaishou.com/xxx` 能正常命中 `kuaishou` 规则 —— 而是服务器配置的
  `plugin.enabled_platforms` 里**没有 `kuaishou`**。`_dispatch` 里
  `candidate.key not in enabled` 会直接 `continue`，而**日志打点在这个判断
  之后**，所以排查时日志里没有任何痕迹。

  **修复**：把这类静默跳过改成显式日志：

  - 未命中任何平台规则 → `[R插件] 链接未命中任何平台规则，跳过: ...`
  - 命中但平台未启用 → `[R插件] xxx 未在 plugin.enabled_platforms 里启用，跳过（可在 WebUI 插件配置里勾选）: ...`
  - 命中但在黑名单 → `[R插件] xxx 在全局黑名单里，跳过: ...`

  （服务器配置已同步加上 `kuaishou`。）

### 新增

- `tests/test_album_send_path.py` 新增「跨容器：视频必须走 base64」一节
  （6 项断言），锁定视频发送方式，防止回退。

## v1.1.5（2026-09-17）

### 修复

- **抖音图集整个发不出来（只剩一条「🔗 识别：抖音」文字）**。这是 v1.1.3 / v1.1.4
  改动引入的**回归**，回滚点在于「图集走 `_send_album` 之后，静态图改成了发
  `Comp.Image.fromURL(直链)`」。

  **根因**：抖音图集的图片直链是 `p3-pc-sign.douyinpic.com/...` 这类**带签名的
  CDN 地址**，有三个坑叠在一起：

  1. 同一个 `url_list` 里有 `.webp` / `.jpeg` 两个变体，**哪一个 403 是随机的**；
  2. 下载必须带 `Referer: https://www.douyin.com/`；
  3. AstrBot 的 `respond.stage` 在真正发出 `Comp.Image.fromURL(url)` 时用的是
     **它自己的下载器**——既不补 Referer、也没有候选回退，**一张失败就抛
     `DownloadFileHTTPError`，整条消息链一起失败**。

  实测矩阵（服务器真实 Cookie，同一个作品内不同项的 403 情况都不一样）：

  | 作品 | 项 | 候选[0] `.webp` | 候选[1] `.jpeg` |
  |---|---|---|---|
  | `GRBfoLTjIu0` | 0 | **403** | 200（123KB） |
  | `j9_O6iEkYYu` | 0 | 200 | **403** |
  | `j9_O6iEkYYu` | 1 | 200 | 200 |
  | `vudrS_L_16Y` | 0 | 200 | **403** |
  | `vudrS_L_16Y` | 1 | **403** | 200 |

  可见**没有任何固定候选顺序可用**，同时 AstrBot 那边一失败就是整条链失败——
  于是用户看到的就是「简介发了，图一张没有」。

  **修复**：图集/图片的媒体**一律先下载到本地再发**，发送端只接收
  `Comp.Image.fromFileSystem` / `Comp.Video.fromFileSystem`，**绝不把远程签名
  直链交给发送端**：

  - `main.py` 新增 `_download_images()` / `_download_album_stills()` /
    `_download_album_videos()`，统一走本插件自己的下载器（`_headers_for` 补
    Referer + `download_many_candidates` 逐个候选回退）；
  - `_send_album()`、`_send_images()` 的**直发档**也改为先下载再发本地文件
    （此前直发档是「不超过 9 张就直接发 URL」，正是用户踩到的路径）；
  - 全部下载失败时**明确发一条失败提示**，不再静默什么都不发。

  **验证**：5 个真实作品（含用户反馈的 `GRBfoLTjIu0`）端到端下载全部成功，
  之前完全发不出的那张现在能拿到 123KB。

### 新增

- `tests/test_album_send_path.py` —— 发送路径的离线回归测试（24 项全通过），
  锁定的核心不变量是「**静态图/动图一律 `fromFileSystem`，发送路径不得出现
  远程媒体直发**」。含静态 AST 断言（禁止 `Comp.Image.fromURL` /
  `Comp.Video.fromURL` 出现在发送方法里）、本地假 HTTP 服务的候选回退行为测试、
  以及用真实 `_send_album` 实现跑的混排顺序与失败提示测试。
- `core/downloader.py` 的候选下载在**全部候选失败**时改为 `warning` 级日志并
  带上候选个数与末次错误——之前是 `debug`，用户反馈「图没发出来」时日志里
  什么都看不到。

## v1.1.4（2026-09-17）

### 修复

- **抖音动图仍然被发成静图**（v1.1.3 没修干净）。v1.1.3 修好了「逐项分流」的
  代码逻辑，但**通道选错了**：我们一直只走 SSR 分享页，而实测 SSR 页面会把动图
  **降级成静态图**。

  **实测对照**（用服务器上的真实 Cookie 抓同一作品 `7676017468716436809`）：

  | 通道 | `aweme_type` | `images[].video` | 能否识别动图 |
  |---|---|---|---|
  | 主接口 `aweme/detail`（a-bogus + Cookie） | **68** | **有完整视频轨** | ✅ |
  | SSR `share/note` | **2** | **`None`（被抹掉）** | ❌ |
  | SSR `share/slides` | 无 `_ROUTER_DATA`，完全拿不到数据 | | ❌ |

  SSR 页面把 `aweme_type` 从 `68` 改写成 `2`，并把每张图自带的 `video` 整个抹掉、
  只留 `url_list` —— 所以在 SSR 路径下怎么改判断逻辑都识别不出动图。原版对
  `share/slides` 也是专走主接口（`apps/tools.js:548`）。

  **修复**：`core/douyin_ssr.py` 新增 `fetch_aweme_by_api()`（主接口 + a-bogus 签名），
  `resolve_by_ssr()` 改为**分场景选通道**：

  - `share/slides` 链接 → **只能**走主接口（该页是客户端 SPA，SSR 无数据）
  - 有 Cookie + 有 node → **优先**主接口（拿完整动图信息）
  - 否则 → 回落 SSR（免登录，但动图会发成静图）

  同时给主接口补上 `DY_SHARE_SLIDES_PAGE` 常量和 `share/slides` 的 ID 匹配规则。

  **验证**：用户反馈的链接（`https://v.douyin.com/j9_O6iEkYYU/`）现在解析为
  **抖音动图**、`album_kinds=['animated','animated']`、2 条视频直链（HTTP 206、
  `Content-Type: video/mp4`），不再是 2 张静态图。

### 新增

- `tests/test_douyin_album.py` —— 动图/静态图分流逻辑的离线回归测试（11 项全通过）。
  覆盖纯静态、纯动图、混排顺序、动图不进图片列表、`aweme_type` 判定、`slides`
  链接识别等容易回退的点。

### 文档

- README 补充说明**为什么图集/动图必须配 Cookie**（SSR 降级动图的实测结论），
  外部工具表新增 `node` 条目及其影响范围。

## v1.1.3（2026-09-16）

### 修复

- **抖音图集里的动图被当成静态图片发出来**：抖音图集可以**混排**静态图和动图，
  动图的标志是**该项自带视频轨**（`image.video.play_addr_h264.uri`），静态图只有
  `url_list`。原版 `processDouyinImageAlbum` 是逐项判断的——有视频轨当视频发，
  没有就当图片发。

  移植版早期按「整条作品有没有动图」一刀切：只要图集里存在一张动图，整条作品就被
  标成「动图」，然后只发视频轨、**所有静态图被静默丢掉**，且动图本身也走了图片链路
  发成静态图片。现在改为 `album_items()` **逐项分流**：

  - 动图 → 按**视频**发送（`Comp.Video`，播放地址顺序与作品一致）
  - 静态图 → 按**图片**发送（`Comp.Image`，带候选 URL 逐个尝试）

  顺序严格按作品原样，新增 `main.py::_send_album()` 统一按 `album_kinds` 还原，
  超过 `max_images` 时仍走合并转发完整发出（动图节点包视频、静态图节点包本地图片）。

- **混合图集被误判为「视频」**：`album_kinds` 逐项统计后，纯动图显示「动图」，
  混排显示「图集（含动图）」，纯静态显示「图集」。

### 优化

- 清理死代码：`has_animated_images` / `animated_image_uris` 被 `album_items` 取代；
  `static_image_urls` / `static_image_candidates` 改为基于 `album_items` 实现，
  自动跳过动图，避免动图混进图片列表被当静态图发。

## v1.1.2（2026-09-16）

### 修复

- **抖音图集部分图片 403**：抖音图集每张图的 `url_list` 有多个 CDN 节点，签名
  时效不一，固定取某个位置的 URL 必然有图下载失败。现在改为**候选 URL 逐个尝试**
  （`static_image_candidates` + `download_many_candidates`），第一个能下载的用，
  实测之前失败的图集 2/2 张全部成功。

## v1.1.1（2026-09-16）

### 新功能

- **抖音评论**：解析抖音作品后，开启配置项 `douyinComments` 即可自动抓取评论，
  以合并转发（聊天记录）形式发出。复用原版 `a-bogus` 签名算法（通过容器内 node
  子进程调用，不重新逆向）。**需要配置抖音 Cookie**（未登录时评论接口返回空）。

> 说明：a-bogus 是抖音旧版算法（browser_version=124），抖音升级算法后评论功能
> 可能失效；评论是附加功能，失败只静默跳过，不影响主解析。

## v1.1.0（2026-09-16）

### 新功能

- **B站评论**：解析 B站视频后，开启配置项 `biliComments` 即可自动抓取热门评论，
  以合并转发（聊天记录）形式发出。未登录也能抓（WBI 签名主接口 + 经典接口兜底）。
  抖音评论暂未支持（依赖 `a-bogus` 签名，见 `platforms/douyin.py` 的说明）。

### 修复

- **抖音图集偶发发不出去**：抖音图片域名有防盗链，下载时缺 `Referer` 头返回 403，
  一张图下载失败会拖垮整条合并转发。现在下载器按域名自动补 Referer
  （抖音 / B站 / 微博 / 快手）。
- **合并转发发送者身份**：图集、评论的合并转发节点，昵称和 QQ 号都改为
  「发起解析的用户」，不再是作者名 + 机器人自己。

### 优化

- 去掉「（共 N 张，已合并为聊天记录）」提示；下载失败的图片直接跳过，
  不再拖垮整条转发；全部失败时降级为直发前几张 URL。

## v1.0.0（2026-09-16）

- 首个发行版：聊天内链接自动解析（抖音 / B站 / 快手 / 微博 / AcFun / 米游社 /
  小黑盒等）、B站扫码登录（`#RBQ` / `#RBS`）、作品缓存（2 小时）、图集合并转发、
  先简介后媒体、并发下载、Cookie 逐项填写（行内表格）。
- 遵循原项目开源协议木兰宽松许可证第 2 版（Mulan PSL v2）。
