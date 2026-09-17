# 更新日志

## v1.4.0（2026-09-17）

**音乐卡片真正可用了**（修的是协议端的一处实现差异），并新增「搜索点歌 +
序号点播」，点歌列表改用图片呈现。

### 修复：音乐卡片发出去显示「发送者版本过低，无法展示内容」

根因**不在插件**，而在协议端处理 OneBot music 段的方式：

1. 协议端不自己生成卡片，而是把参数 POST 给一个外部「音卡签名服务」，
   再把返回的 JSON 当 lightApp 发出。QQ 会校验 config.token，
   不合法就统一回「发送者版本过低，无法展示内容」（这是**通用验证失败提示**，
   不是字面意义的版本问题）。
2. 官方 NapCat 的首选签名服务是 http://106.55.0.102:10087/
   （见其源码 packages/napcat-onebot/api/msg.ts），ss.xingzhige.com
   只是备选，且它的 id 模式已被关闭。
3. 该服务返回的是**双重编码**响应（形如 "{...}"，外层多一层引号）。
   官方 NapCat 用 HttpGetJson<string>() 会先解析一次，所以正常；
   **SnowLuma 直接用 resp.text()**，于是 JSON.parse 得到 string
   而不是 object，触发 field "text" must contain a JSON object
   校验失败 —— 然后它**降级成字段残缺的本地卡片**，被 QQ 判为版本过低。

**修法**：插件内置一个极小的签名代理（core/music_sign_proxy.py，
默认监听 18888），把响应展开一层再交回协议端。上游失败会自动换备选地址。
配置里可改端口与上游地址。

> 部署后需要把协议端的 musicSignUrl 指向 http://astrbot:18888/
> （写在 config/onebot_<uin>.json 或全局 config/snowluma.json），
> 并重启协议端容器。

### 修复：卡片平台标识张冠李戴

签名服务固定写 meta.music.tag = "QQ音乐"，于是点网易云的歌也会显示
「QQ音乐」标签（但点击跳转是对的，显得自相矛盾）。代理现在按 jumpUrl
域名判断真实来源平台，改写 tag / tagIcon。

### 修复：卡片改回 custom 模式

id 模式（type=163/type=qq + id）已被签名服务弃用（上游对 id
模式直接返 HTTP 400「缺少 title」，旧服务返回纯文本「关闭id解析功能」）。
现在统一走 custom，且 url/audio/title/image 四项齐全
（官方源码会逐个校验，缺一即丢弃整条消息）。

### 新增：搜索点歌 + 序号点播

- 点歌 歌名 → 搜到后发一张**列表图**（默认 10 首），60 秒内回复序号
  即播放对应歌曲
- 列表图用 Pillow 现画（core/music_card_image.py）：平台色条 + 封面 +
  序号 + 歌名/歌手/专辑，无二维码
- 与原来的「直接送」并存：配置 music.searchMode 可在 list /
  direct 之间切换
- 序号点播只在「该会话 60 秒内搜索过」时才响应，不会干扰群里的普通数字消息

### 新增配置项（点歌分组）

| 项 | 默认 | 说明 |
|---|---|---|
| searchMode | list | 列表图+序号 或 直接送 |
| enableSignProxy | true | 音乐卡片签名代理开关 |
| signProxyPort | 18888 | 代理端口 |
| signProxyUpstream | 空 | 上游签名服务（留空用内置默认） |

## v1.3.2（2026-09-17）

修卡片 + 优化指令格式。

### 指令格式：平台可以写在「点歌」前面了

原来只认 `#点歌 网易云 晴天`（平台在中间），现在主推平台前置：

```
点歌 晴天            → 用配置里的「默认平台」
网易云点歌 晴天       → 强制走网易云
QQ点歌 晴天          → 强制走 QQ 音乐
```

- 平台写在「点歌」**前面或后面都认**（`#点歌 网易云 晴天` 依然有效，向后兼容）
- `#` / `/` 前缀变成可选
- `QQ` 不分大小写（`qq点歌` / `Qq点歌` / `QQ点歌` 都认）
- 平台和「点歌」之间可以有空格（`网易云 点歌 晴天`）

实现上把正则提成了共用常量
（`core/constants.py::MUSIC_COMMAND_PATTERN`），`COMMAND_RULES` 和
`main.py::cmd_music_search` **共用同一份** —— 两份正则各写一份的话，很容易
出现「命令命中了但解析不出关键词」这种最难查的问题。测试里有一条断言专门
锁这个（`COMMAND_RULES 与 main.py 共用同一份正则`）。

### 卡片：改用 QQ 音乐官方类型 + 修掉重复发送

**问题一：卡片发出来提示「发送者版本过低不展示」**

原因是 `music` 段的 `_type` 取值。实测三种类型的 QQ 兼容性：

| `_type` | 说明 |
|---|---|
| `qq` | **QQ 音乐官方卡片 —— 最可靠**，带播放按钮 |
| `163` | 网易云卡片，**部分 QQ 客户端提示「版本过低」** |
| `custom` | 自定义卡片，需要 ark 签名，QQ 基本不认 |

原来 QQ 音乐走的是 `custom`，现在改成 **`qq` + 数字 songid**
（从搜索结果的 `id` 字段取，不是 `mid`）。

顺带一个收益：**官方卡片只需要 songid，不需要音频直链** ——
少发一次 `CgiGetVkey` 请求，也就少一次撞 QQ 音乐随机限流的机会。

**问题二：会重复发送**

原来 card 模式会发「1 条文字 + 最多 3 张卡片」= 4 条消息。现在：

- card / voice / file **一律只发 1 首**（每条都是独立消息，多发就是刷屏）
- card 模式**不再发多余的说明文字**（卡片本身带歌名歌手）

### 其它

- `Song` 的 `extra` 新增 `songid`（QQ 音乐的数字歌曲 ID）
- 修复 `MUSIC_COMMAND_PATTERN` 定义位置（原先在 `COMMAND_RULES` 之后，
  引用时会 `NameError`）
- `tests/test_music_search.py` 的命令正则测试扩到 21 个用例，覆盖
  前置/后置/无平台/大小写/空格等写法

## v1.3.1（2026-09-17）

**修复 v1.3.0 会导致 Cookie 丢失的严重问题。**

### 问题

v1.3.0 声称「已有配置会自动迁移，Cookie 不会丢」，**实际是错的** ——
实测升级后 `music.neteaseCookie` 和 `music.qqMusicCookie` 都变成了空值。

### 根因

**AstrBot 会在插件代码能读到配置之前，按 schema 把配置里 schema 不存在的键全部删掉**
（顶层分组和分组内的键都删）。

在服务器上插探针键验证过：

```
插入 probe_zzz_not_in_schema（顶层 + music 分组内各一个）
重启
-> 顶层探针 ❌ 被删除
-> music 里探针 ❌ 被删除
```

所以 v1.3.0 的做法（从 schema 里删掉旧键，然后靠 `migrate_music_config()`
读旧值搬到新位置）**根本行不通**：迁移代码跑的时候，旧值已经被框架清掉了，
`conf.get("netease")` 拿到的是 `None`，于是什么都没搬。

### 修复

**在 schema 里保留承载用户数据的旧键**，这样框架就不会清理它们，
迁移代码才能读到并搬走：

- `netease` 分组保留 4 项：`useNeteaseSongRequest` / `songRequestPlatform` /
  `songRequestMaxList` / `neteaseCookie`（都标注为「已废弃」）
- `other.qqMusicCookie` 保留

分组描述里写明了「这一组是旧位置，插件会自动搬运并清空，可以忽略」，
**下个版本会移除**。

真正没人用的垃圾项仍然删掉了：`isSendVocal`（未移植的发语音开关）/
`useLocalNeteaseAPI` / `neteaseCloudAPIServer`（自建 API）/
`neteaseCloudCookie` / `neteaseCloudAudioQuality`（云盘）/
全部 `kugou*` / `qqMusicAudioQuality`。

### 已经升到 v1.3.0 的用户

Cookie 需要**重新填一次**（在「点歌」分组里）。之后从 v1.2.0 直接升到 v1.3.1
的路径是完好的，不会再丢。

## v1.3.0（2026-09-17）

**配置重组 + 点歌发送方式可切换**。点歌配置从「网易云音乐」分组里独立出来
成为单独的「点歌」分组，顺手清掉了随原 Guoba 面板带过来、本移植版从未使用
（或已失效）的一堆配置项。

> ⚠️ **这个版本的自动迁移是失效的，会把 Cookie 清空**，
> 请直接使用 v1.3.1（原因与修复见上）。

### 新增：点歌发送方式可切换

新增配置项「发送方式」，四个选项：

| 模式 | 效果 | 适合 |
|---|---|---|
| `link`（默认） | 列出「歌名 - 歌手 + 播放页链接」 | 想自己挑，最稳、零额外请求 |
| `card` | 发**音乐分享卡**（最多 3 首） | 群里直接点播放 |
| `voice` | 下载后发**语音条** | 短音频（见下方限制） |
| `file` | 以群文件形式发音频 | 想听整首 |

### 关键技术发现（都经服务器实测）

- **`Comp.Music` 的 `_type` 只能用 `object.__setattr__` 设置**。这个字段是
  pydantic v1 的「非字段」属性：

  - 构造时传 `_type="163"` → 被**静默忽略**（不在模型字段里）
  - 实例化后 `comp._type = "163"` → `ValueError: "Music" object has no field "_type"`
  - 只有 `object.__setattr__(comp, "_type", "163")` 能写进 `__dict__`

  而且 AstrBot 的 `respond/stage.py` **有专门校验器读它**：

  ```python
  Comp.Music: lambda comp: (
      (comp.id and comp._type and comp._type != "custom")
      or (comp._type == "custom" and comp.url and comp.audio and comp.title)
  )
  ```

  `_type` 缺失时**读取本身就抛 `AttributeError`**，整条消息链发不出去。
  本插件用 `_music_card()` 统一封装：网易云走 `_type="163"` + `id`，
  QQ音乐走 `_type="custom"` + `url/audio/title`。

- **`Comp.Record` 会把音频转成未压缩 WAV，且无法绕过**。
  `convert_to_base64()` 里 `target_format="wav"` 是**写死**的：

  ```python
  return await MediaResolver(file_source, media_type="audio",
                             default_suffix=".wav").to_base64(target_format="wav")
  ```

  也就是 44100Hz / 16bit / 单声道 ≈ **88.2KB/秒**，base64 后还要再涨 1/3
  → 约 **117KB/秒**。实测 30 秒音频 → 3.5MB payload。

  所以「语音条」天生只适合短音频：一首 4 分钟的歌约 **28MB payload**，
  协议端基本收不下。本插件按 `song.duration` 预估体积，
  超过 90 秒就**明确降级到链接并提示**，而不是发出去让用户干等。

- **想发整首就用 `file` 模式**：`Comp.File(name=..., url=直链)` 生成的
  payload 只有 `{"type":"file","data":{"name":...,"file":"<直链>"}}`，
  **协议端自己去拉**——既不受 WAV 转换影响，也不占本地带宽。

### 配置重组

**新增「点歌」分组**（6 项）：开启点歌 / 默认平台 / 列表长度 / 发送方式 /
网易云Cookie / QQ音乐Cookie。

**删除的分组与配置项**：

| 位置 | 项 | 原因 |
|---|---|---|
| `netease`（整组） | `isSendVocal` | 未移植：发语音走 NTQQ 私有协议 |
| | `useLocalNeteaseAPI` / `neteaseCloudAPIServer` | 未使用：自建 NeteaseCloudMusicApi |
| | `neteaseCloudCookie` / `neteaseCloudAudioQuality` | 未移植：云盘命令 |
| `other` | `kugouApiServer` / `kugouCookie` / `kugouCookieFields` / `kugouAudioQuality` | 酷狗已移除 |
| | `qqMusicAudioQuality` | 未引用：音质由取直链时的档位决定 |
| 命令规则 | `#rns` / `#rnq` / `#rks` / `#rkq` | 依赖的自建服务都没实现，留着只会一直刷警告 |

点歌平台选项里的**酷狗已移除**（老取直链接口恒返 `err_code=30020`，
可用替代拿不到可分享的直链）。

### 迁移（⚠️ 此机制在本版本失效，v1.3.1 才修好）

`core/config_migrate.py` 新增 `migrate_music_config()`，在插件加载时
把旧路径的值搬到 `music.*`：

```
netease.useNeteaseSongRequest  ->  music.enable
netease.songRequestPlatform    ->  music.platform   （kugou 自动回退 netease）
netease.songRequestMaxList     ->  music.maxList
netease.neteaseCookie          ->  music.neteaseCookie
other.qqMusicCookie            ->  music.qqMusicCookie
```

规则是「**新位置还停在默认值才用旧值覆盖**」——如果用户已经在新位置设过，
保留新值。搬迁后旧键一律清除，`netease` 组空了就整组删除。

迁移过程**幂等**（插件重载会多次触发），有 24 项离线断言覆盖
（`tests/test_music_config.py`）。

### 其它

- 酷狗链接解析（发酷狗分享链接）改为**明确的未支持提示**，
  并引导用户改用 `#点歌`，不再要求自建 API 服务。
- `Song` 新增 `duration` 字段（网易云 `dt`、QQ音乐 `interval`），
  用于语音模式的体积预估。

## v1.2.0（2026-09-17）

新增 **点歌搜索**：`#点歌 歌名` 搜歌并列出候选（歌名 + 歌手 + 播放页链接）。
支持网易云 / QQ音乐，两者**取直链都经过真实会员账号实测**。

### 新增

- **`#点歌 <关键词>`** —— 搜索并列出候选（默认取配置里的平台）。
  命令里可指定平台：`#点歌 网易云 晴天` / `#点歌 QQ音乐 晴天`。
  列表长度由「点歌列表长度」配置控制（默认 10，上限 20）。

  输出只发文字 + 网页链接，**不发音频本体**——不占带宽、不用转码，
  QQ 音乐配了会员 Cookie 后链接点开就是完整版。

  平台选择逻辑：命令里写了前缀就只搜那个平台（尊重用户选择，不 fallback）；
  没写则用配置的 `songRequestPlatform`，**该平台无结果时自动试另一个**。

- **`core/music_search.py`** —— 新模块。移植自 TRSS-Yunzai 的
  `xiaofei-plugin`（`apps/点歌.js`）的多平台抽象思路，但修正了它几处
  **已经失效**的取数路径（详见下）。

### 关键实测结论（这些决定了实现方式）

- **QQ 音乐搜索的取数路径已过期**。接口返回的歌曲现在装在
  `search.data.body.item_song`（数组）里，而 `search.data.body.song.list`
  恒为空数组。`xiaofei-plugin` 取的是后者（`apps/点歌.js:2250`），
  所以**它的 QQ 音乐搜索现在永远返回空**——不是接口失效，是字段变了。
  本插件两个字段都试，优先 `item_song`。

- **QQ 音乐取直链必须登录态 Cookie，且必须传 `filename` 才能拿到高音质**。
  匿名调 `CgiGetVkey` 恒返回 `result=104003`（= 需要登录/VIP）；实测带会员
  Cookie 后周杰伦《晴天》可拿到 320kbps mp3。而**不传 `filename` 会静默降级
  到 96kbps m4a**，所以本插件按「320mp3 → 192ogg → 128mp3 → 96aac」
  从高到低挑第一个该曲目有资源的档位。

- **不能给 `u.y.qq.com` 发 `Accept-Language` 头**。逐项隔离实测：带上它接口
  固定返回 `search.code=2001` + 空 body；其余头（UA / Content-Type /
  Referer / Origin / Accept）都无影响。本插件用独立的「干净头集合」，
  避免 `core/http.py` 的 `BROWSER_HEADERS` 混进来。

- **QQ 音乐的 2001 拒绝有随机性，重试才有效**。对照实验（同请求连发多次）：

  | 条件 | 结果 |
  |---|---|
  | urllib 连发 3 次 | ✅ / ❌ / ❌ |
  | aiohttp 每次新建 session 连发 3 次 | ❌ / ❌ / ❌ |
  | aiohttp 复用 session 连发 3 次 | ❌ / ✅ / ✅ |

  与连接方式、header、Cookie 均无关，判断是服务端多节点、部分节点限流。
  所以**不是靠降频，而是靠重试**：默认重试 3 次、退避 1.5s 起。
  实测 6 个关键词成功率 **5/6（83%）**、平均耗时 2.2s。

- **搜索缓存是对抗限流最有效的一招**：成功一次缓存 10 分钟，期间同关键词
  直接返回（实测 0.0ms），既不打扰接口也不会让用户看到限流失败。

- **网易云完全匿名可用**。搜索和取直链都不需要 Cookie；配 `MUSIC_U` 后才能
  解锁 VIP 歌曲的高音质直链（实测会员账号 `vipType=11` 生效）。

### 配置

用的是原 Guoba 面板就有的字段，**无需新增配置项**：

- `netease.useNeteaseSongRequest` —— 点歌总开关（默认 false，需手动打开）
- `netease.songRequestPlatform` —— 默认平台（`netease` / `qq`）
- `netease.songRequestMaxList` —— 列表长度（默认 10）
- `netease.neteaseCookie` —— 网易云 Cookie，格式 `MUSIC_U=xxx`
- `other.qqMusicCookie` —— QQ 音乐 Cookie，**整串粘贴即可**

QQ 音乐 Cookie 的字段别名会自动映射（实测确认的对应关系）：

| 浏览器里常见名 | 接口要求名 |
|---|---|
| `uid` | `uin` |
| `qqopenid` | `psrf_qqopenid` |
| `qqyunionid` | `psrf_qqunionid` |
| `qqaccess_token` | `psrf_qqaccess_token` |
| `qm_keyst` | `qqmusic_key` |

> ⚠️ `qqmusic_key` 有效期约 **12 小时**，过期后取直链会重新失败。
> 届时重新抓一次 Cookie 即可，这是 QQ 音乐的机制。

### 未实现

- **酷狗**：搜索匿名可用，但老取直链接口（`wwwapi.kugou.com/play/songinfo`）
  现在恒返 `err_code=30020`，可用替代返回的是音频流本体、没有可分享的
  播放页链接，不适合「发链接」这种交付方式。
- **语音发送**：`xiaofei-plugin` 发语音走 NTQQ 私有协议
  （`e.bot.sendUni("PttStore.GroupPttUp")` + 自建 IP:port 上传），
  AstrBot / aiocqhttp 没有这套接口，无法移植。音乐分享卡同理
  （`OidbSvc.0xb77_9`）。

## v1.1.7（2026-09-17）

性能优化版。核心目标：**缩短从「发出链接」到「看到媒体」的等待时间**。
三项优化实测合计省下约 **0.5–1.5 秒/次解析**，且不改变任何功能行为。

### 优化

- **HTTP 连接池复用（提速约 1.8x）**。此前每个 HTTP 请求都新建
  `TCPConnector` + `ClientSession`，而且建在 **retry 循环内部**——一次重试就
  再建一套。代价是每次都重做 TCP 三次握手 + TLS 协商，连接池形同虚设。
  B 站一次解析要发 3–4 个请求、抖音要发 6 个（签名 + 主接口 + 4 档画质探测），
  全部各建一套。

  现在改为模块级 lazy 单例 session（`core/http.py::get_session()`），所有请求
  共用连接池，retry 只重试请求本身。**实测：8 次同 host 请求 785ms → 433ms**
  （1.81x）；8 个并发请求仅 86ms。

  两个细节：

  - 用 `DummyCookieJar` 且 **不** 在 session 级存 Cookie——插件自己把 Cookie
    塞进 header，避免跨请求串味（比如抖音的 ttwid 污染 B 站请求）。
  - 检测到事件循环变化会自动重建 session（插件重载 / 测试里的新 `asyncio.run`
    都会产生新 loop，直接复用会报 `Event loop is closed`）。
  - `fetch_with_cookies` 例外：它必须收 `Set-Cookie`（B 站扫码的 SESSDATA 靠
    这个下发），所以用独立 CookieJar；但连接器复用共享池，仍然享受 keep-alive。

- **a-bogus 签名改常驻 node 进程（提速 165x）**。此前每次生成签名都起一个
  node 子进程：node 冷启动 40–80ms + `require` 460 行混淆 JS 再 40–120ms，
  **而签名本身不到 5ms**——95% 以上的时间花在「把引擎热起来」上，而且签一次
  热一次，永远热不起来。

  新增 `core/a_bogus_worker.cjs`：node 常驻，脚本只加载一次，之后走
  stdin/stdout 行协议复用同一个 V8 实例。**实测：269.3ms → 1.6ms/次（165x）**。

  worker 起不来时（node 缺失、容器限制、脚本损坏）**自动降级**回一次性子进程，
  功能不受影响，只是慢一点。

- **抖音画质探测改并发**。`probe_qualities` 以前 `for ratio: await probe(...)`
  串行探 4 个画质档，每个 15s 超时——每次解析白等 2–4 个 RTT。现在用
  `asyncio.gather` 一起发，**再按原顺序去重**，所以选出的最优档位与串行版本
  完全一致。

- **base64 编码移出事件循环**。视频发 base64 是跨容器的硬性要求，但
  「读文件 + 编码」是同步阻塞的，一个 70MB 视频峰值内存约 100MB、耗时数百
  毫秒，期间 AstrBot 所有协程都被卡住（包括其它会话）。现在丢到
  `asyncio.to_thread` 执行，`_video_component` 相应改为 `async`。

- **正则预编译缓存**。`match_rule` 每条 URL 都要顺序试最多 24 条规则，原来
  每次调 `re.search(pattern_str, ...)`。现在按 pattern 串缓存编译结果
  （`core/constants.py::_compiled`），命中后直接调 `Pattern.search`。

### 说明

- `core/general_adapter.py` 的「第三方接口按优先级逐个轮换」**保持串行**：
  这是有意的设计取舍。并发会同时对多个第三方接口施压，容易触发风控，
  而这类接口本身就是「谁先能用谁上」的兜底角色。
- 插件卸载（`terminate`）时会关停常驻 node 进程并关闭共享连接池，
  不留僵尸进程和悬挂 socket。

### 新增

- `tests/test_a_bogus_worker.py`——常驻 worker 的正确性 + 耗时对比测试。
- `tests/test_http_pool_bench.py`——连接池复用的真实网络耗时对比。
- `tests/test_album_send_path.py` 新增 3 项断言：`_video_component` 必须是
  `async`，且**每个调用点都必须带 `await`**（漏了会静默拿到 coroutine 对象，
  表现为「视频发不出去但不报错」，极难排查）。

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
