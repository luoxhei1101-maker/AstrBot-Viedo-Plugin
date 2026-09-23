# 更新日志

## v1.6.9（2026-09-23）

**官机点歌语音：从「成功要等 35 秒 / 失败干等 94 秒」到「约 21 秒 / 32 秒拿到链接」。**

### 症状

用户反馈「语音发的好慢」。日志实测：

```
15:28:46  语音预转码完成
15:29:21  首次上传失败（+42s）
15:29:48  重试 2（+69s）
15:30:12  重试 3（+93s）
→ 最终什么都没发出去
```

### 三层根因（都不是「算法慢」）

1. **silk 编码同步阻塞** —— 适配器 `to_path(target_format="tencent_silk")` 里，
   `wav_to_tencent_silk()` 虽然是 `async def`，内部却直接调 `pysilk.encode()`，
   一首 4 分半的歌会把 event loop 堵住约 20 秒。
2. **上传超时不可控**（这才是「慢」的主因）—— botpy 的 `BotHttp.timeout` 是
   **实例级**属性，`request()` 没有 per-request 超时参数；超时后
   `except asyncio.TimeoutError` 直接返回 `None`，被 `APIReturnNoneError` 接住，
   交给 tenacity 重试 3 次（退避 2/4/8 秒）。
   总账 = silk 20s + 15s×4 + 14s ≈ **94 秒，然后什么都没有**。
3. **官机不走 base64 那条路** —— v1.6.7 的「预转 16k wav 让框架跳过转码」
   只对 OneBot 的 `convert_to_base64` 有效；官机走的是
   `to_path(target_format="tencent_silk")`，照样要自己 ensure_wav → silk。

### 做法

**① `complexity=0` —— silk 编码快 3 倍**

`pysilk.encode(..., complexity=2, ...)` 是默认值。实测（同一段音频）：

| complexity | 120s 音频 | 270s 音频 | 体积 |
|---|---|---|---|
| 2（默认） | 9.69s | ~22s | 250.9 / 375.4 KB |
| 1 | 5.50s | — | 242.2 KB |
| **0** | **3.11s** | **7.1s** | **180.7 / 273.8 KB** |

点歌语音条本来就不是高保真场景（QQ 语音本身就是窄带），又快又小都划算。

**② 插件自己转、自己传 —— 超时和重试交给插件决定**

新增 `core/qq_voice.py`（本地 silk 编码，**同步函数**，调用方必须 `to_thread`），
以及 `main._upload_qq_media` / `main._send_qq_voice`。发送复用既有的
`_send_qq_payload`（绕过适配器直接打 HTTP 接口）。

**③ 上传超时按「冷却后首次」定，不能拿连续测出来的数字当依据**

实测（414KB silk）：

| 场景 | 耗时 | 结果 |
|---|---|---|
| 静置 150 秒后首次上传 | **8.28s** | ✅ |
| 紧接着 20 秒后再传 | 46.36s | ✅ |

而且**与体积无关**（561KB 6.52s / 293KB 44.83s / 250KB 35.59s）——
腾讯侧对**连续**上传会排队。用户点歌是零散的，正常命中「首次」那一档，
所以超时定 **25 秒**（三倍余量）。

重试也分情况：**只对「秒失败」（<6 秒）重试一次** —— 耗满超时的说明被排队了，
再试只会更慢，还会加剧别人的排队。

### 收益（4 分半的歌，全部实测）

| 环节 | 改前 | 改后 |
|---|---|---|
| 下载 mp3 | 1.7s | 1.7s |
| mp3 → 16k wav | 7s | 3.9s |
| silk 编码 | ~22s | **7.1s** |
| 上传 | 15s 超时 × 3 次重试 | **8.3s** |
| **成功总耗时** | **~35s** | **~21s** |
| **失败总耗时** | 94s 后**什么都不给** | **~32s 后给链接** |

### 顺带修的

- **`#R配置 cookie <平台> 强制 <整串>` 的回执缺提示** —— 强制写入会跳过字段检查，
  但回执里那句提醒是死的：`tail = "" if force else ""` 恒为空串，用户会误以为
  「保存成功 = 凭据没问题」。
- 清理 12 处未使用导入（ruff `F401` / `F811` 归零）。
- `_music_to_voice_wav` / `_send_qq_voice` 的文件 `stat` 加了 `OSError` 防护
  —— 临时文件刚好被清掉时，不该让一行日志把这次发送带崩。

### 测试

新增 `tests/test_qq_voice.py`（27 项断言）。其中 **B 段是真跑 `pysilk`**：
除了验证「正确规格能出 silk 且以 `0x02` 开头（tencent 标志）」，还逐一确认
44.1k / 立体声 / 8bit / 空音频**必须被拒** —— 宁可降级发链接，也不在热路径上
偷偷重采样（那是个新的静默失败点）。

另锁住三条最容易退化的源码不变量：silk 编码必须走 `asyncio.to_thread`、
`ENCODE_COMPLEXITY == 0`、上传失败必须降级。

> 本地开发可 `pip install silk-python`（import 名是 `pysilk`），
> 装完 silk 编码就能进单元测试，不用非得到容器里跑。

**全量 23 个测试文件通过。**

## v1.6.8（2026-09-23）

**新增：分协议端配置 —— OneBot v11 和 QQ 官方机器人各读自己那一份。**

### 起因

同一份配置里混着两套平台的选项，改一边永远影响另一边：

| | OneBot v11 | QQ 官方机器人 |
|---|---|---|
| 合并转发（聊天记录） | ✅ | ❌ 适配器没这个消息段 |
| 音乐卡片 | ✅ | ❌ |
| 原生 markdown + 消息按钮 | ❌ | ✅ |

所以「用聊天记录发送」「点歌发送方式=音乐卡片」在官机上**怎么调都发不出去** ——
之前只能靠代码里散落的 `if 官方机器人` 硬兜，用户看到的是「配置项写了但不生效」。

### 做法

新增顶层配置分组 `profiles`，**每个协议端一份**：

```
profiles
├── mode            ← 配置来源：per_platform（默认）/ shared
├── onebot          ← OneBot v11 专用（12 项）
├── qqofficial      ← QQ 官方机器人专用（11 项）
└── fallback        ← 其它协议端（9 项）
```

**关于「选项卡」**：AstrBot 的插件 schema 只支持
`int / float / bool / string / text / list / file / object / template_list / dict`，
**没有 tabs 控件**；但 `object` 可以嵌套，前端渲染成**可折叠分组**，
所以用「一个协议端一个嵌套 object」当选项卡的等价形态。

平台专属的选项只出现在该出现的那一组里：官机组**没有**
`send_as_forward` / `album_forward_when_exceed` / `enable_sign_proxy`
（配了也不生效，不如不显示）；OneBot 组**没有** `md_image_width` / `qq_buttons`。

### 取值规则（顺序很重要）

1. `mode=shared` → 全部读旧位置（`plugin.*` / `music.*`）；
2. `mode=per_platform`（默认）：
   a. 面板里的值**不等于该组默认值** → 用户改过 → 用它；
   b. 面板还是默认值 → **看旧键改过没有**，改过就继承旧键；
   c. 两边都没动过 → 用该组默认值。

第 2b 步是关键：**没有它，升级会把老用户调好的
`plugin.send_as_forward=true` 静默忽略掉**，表现是「我明明开了合并转发，
怎么不收聊天记录了」。各组的默认值也都取「当前实际生效的行为」，
所以升级本身不改变任何人的行为。

### 能力兜底：偏好不能盖过能力

`_music_send_mode` 现在按协议端能力再兜一层 —— 官机上就算配置是 `card`
（音乐卡片），也自动降级成 `voice`。**这条不是可选项**：`conf_plat` 会把
用户旧配置里的 `music.sendMode=card` 继承到官机那份，没有兜底官机就会
拿着 `card` 去发一个它做不到的形态，结果是整条消息失败。

### 写入也要分平台（容易漏）

`#R配置 形式 聊天记录` 这类命令是按旧路径写的。分协议端模式下如果还写旧键，
就是「命令回了成功、行为没变」—— 最难查的一类 bug。所以 `_save_conf`
加了协议端重定向，回执里也会写明「只影响 XXX 这份配置」。

### 新增命令

```
#R配置 协议端      ← 看当前机器人这份配置的全部生效值（配置来源 + 各项）
#R平台            ← 简版：能力 + 配置来源
```

### 顺手清掉的死代码

`plugin.platformProfiles`（v1.6.7 埋的「按实例覆写能力」）**从来没生效过** ——
它的键不在 `_conf_schema.json` 里，而 AstrBot 会在插件代码执行前按 schema
裁剪配置，所以读出来永远是空。这正是「schema 里没有的键等于不存在」那条铁律，
已删除并由新分组取代。

### 测试

新增 `tests/test_platform_profiles.py`（80+ 项断言），除行为外还锁了四条
最容易退化的不变量：

- **代码字段 ↔ schema 键一一对应**（以后只改一边立刻红，防止配置被框架静默裁掉）；
- `SHARED_DEFAULTS` 与 schema 里旧键的默认值一致（不一致会让「改没改过」判断失准）；
- 各组分组的默认值与 schema 一致（不一致会让面板显示值 ≠ 插件实际用的值）；
- `False` / `0` 是**有效配置**，不能被当成「用户没设」。

另给 `test_r_config.py` 补了写入重定向的断言（含官机拒绝 + 两份配置隔离），
给 `test_qq_buttons.py` 补了宽度配置的来源断言。

## v1.6.7（2026-09-22）

**修复：评论区里的「动图」（表情包）现在能提取了。**

### 问题

v1.6.6 让评论图片能发了，但用户说「评论区里有动图评论」，怎么都找不到。
翻了几百条评论、逐张验了图片格式，全是 `image/jpeg` —— **动图根本不在
`image_list` 里**。

### 根因：动图藏在独立的 `sticker` 字段里

抖音评论的「表情包」是**独立字段 `sticker`**，既不在 `image_list`
也不在 `video_list`：

```json
"sticker": {
  "id": 7667238231015948297, "width": 344, "height": 240,
  "static_url":  {"uri": "...", "url_list": [...]},
  "animate_url": {"uri": "...", "url_list": [...]},   ← 动图版
  "sticker_type": 2,
  "origin_package_id": -4156610121569672
}
```

实测（作品 `7687666222385355867`，210 条评论里 **41 条带 sticker**）：

| 评论 | sticker 类型 | 结果 |
|---|---|---|
| peach猹「用别人的小猫火了为啥不艾特原主人」 | `image/gif` | **2133 KB / 344×240 / 123 帧** |
| 真理「第二张怎么还有海豚叫…」 | `image/png` | 278 KB 静态 |
| 爱别离「这么多点赞量…」 | `image/jpeg` | 33 KB 静态 |
| 肆叁.「能不能让卖家多发两个大肥猫」 | `image/webp` | 4.5 KB 静态 |

也就是说**光看字段名判断不出是不是动图**（`static_url` 和 `animate_url`
实测常常返回同一个 URL），最终还是靠下载后的内容检测。

### 修复

1. `core/douyin_comment.py` 新增 `_sticker_candidates()`：
   按 `animate_url` → `static_url` 取候选，并把它**并入** `images` 一起返回。
2. 因为发送端早已是「统一下载 → 检测是否动图 → 分别处理」，
   所以 **`main.py` 一行没改**：GIF 自动被认成动图、转 mp4、单独成一条；
   静态 sticker 跟文字同一条（文字在上）。

### 实测效果

```
--- peach猹 | '用别人的小猫火了为啥不艾特原主人'
    图1 候选 3 个: .../obj/tos-cn-o-0812/oUZAeCE4...?sc=sticker_heif
       -> .gif 2133 KB  344×240  帧=123  判定=动图
         转 mp4: 611 KB  <- 作为一条视频单独发
```

同批 10 条带图评论：**动图 1 张 / 静态图 9 张**，判定全部正确。

### 测试

`tests/test_comment_image.py` 新增 A2 组（12 项断言）：`animate_url` 优先、
退回 `static_url`、异常结构不炸、**纯 sticker 评论（无文字无 `image_list`）
不再被丢**、`image_list` 与 `sticker` 同时存在时两个都收。
全量 19 个测试文件通过。

> 📌 顺便把 `deploy_rconsole.py` 的部署校验清单补全了（加上
> `core/douyin_comment.py` / `core/bili_comment.py` / `core/media.py`），
> 以后改这几个文件也会被 md5 校验覆盖。

## v1.6.6（2026-09-22）

**新增：评论里的图片可以提取出来了（含作者常发的纯图评论）。**

### 问题

用户问「视频链接里作者的图片评论能不能提取出来」。查代码发现**两个 bug**，
而第二个正好命中「作者只发图不配字」这种最常见的情况：

1. 评论节点只发 `Comp.Plain(text)` —— 图片字段（抖音 `image_list` /
   B站 `content.pictures`）**压根没读**；
2. 更糟的是 `if not text: return None` —— **纯图片评论被整条丢掉**。

实测用户给的那条作品（`7688010612275061413`）评论共 2 条，作者那条 `text`
正好是空串，于是**永远看不到**。

### 实测数据（2026-09-22，抖音评论图字段）

| 字段 | 体积 | 尺寸 | 结论 |
|---|---|---|---|
| `origin_url` | 660 KB | 1600×1600 | **原图，用它** |
| `medium_url` | 88 KB | 332×332 | 缩略 |
| `thumb_url` | 21 KB | 124×124 | 缩略 |
| `crop_url` | 29 KB | 124×166 | 裁切缩略 |
| `download_url` | — | **403** | **别碰** |

`origin_url` 有 4 个 CDN 候选，后缀分 `.image`（PNG 804 KB）和 `.jpeg`
（660 KB）—— 都是 1600×1600 原图。所以直接复用了图集那套
`rank_image_candidates`（`.jpeg` 优先），顺手把体积也省了。

### 修复

1. `core/douyin_comment.py`：新增 `_comment_image_candidates()`，从
   `image_list[].origin_url.url_list` 取候选并排序；`_normalize_comment`
   改成**「文字或图片有一个就保留」**，返回结构加 `images`。
2. `core/bili_comment.py`：同样处理 `content.pictures`（B 站是单地址，
   包成单元素列表，和抖音共用同一套发送逻辑）。
3. `main.py`：新增共用的 `_build_comment_nodes()`，两个平台都改用它。

### 排布规则（对齐用户要的观感）

- **静态图 + 文字** → 文字在上、图片在下，放**同一个节点**；
- **动图** → 转成 mp4 后**单独成一条**（文字另起一条）；
- 纯图评论（`text` 为空）也正常生成节点，正文只留一行 meta。

图片一律**先落盘再发**（`download_media_candidates` 走候选回退），
理由见 `_download_album_stills`：抖音图片直链需要正确 Referer 且有多候选，
AstrBot 发送端的下载器两者都不具备，直发会**整条消息链一起失败**。

### 动图：接口层没有任何标记，只能看文件本身

这里有个**实测结论**要记一下：抖音评论的动图在接口层面**没有任何标识** ——
`image_list` 的字段和静态图**完全一样**（没有 `is_animated` / `type` 之类），
而且扫了 **28 个作品 / 955 条评论**，`video_list` **全部是 `None`**，
URL 里也没有 `.gif` / `.mp4` 痕迹。

所以判断方式改成**下载后看文件内容**（`core/media.is_animated_image`，
用 Pillow 读 `is_animated` + `n_frames > 1`）。转 mp4 的要点：

- `scale=trunc(iw/2)*2:trunc(ih/2)*2` —— h264 要求偶数尺寸，
  GIF 常见奇数尺寸，不补齐会被 ffmpeg 拒绝（实测 101×77 的 6 帧 GIF
  转出 h264 / 100×76 / 6 帧）；
- `-pix_fmt yuv420p` —— 不带某些播放器放不出来；
- `-movflags +faststart` —— moov 挪到文件头，发送端能边下边播；
- `-an` —— 动图没音轨，显式声明免得塞空音轨。

转码失败时**退回按静态图发**（总比什么都不发好）。

> ⚠️ **动图这条路径还没有真实样本验证过**：合成 GIF 的转码链路是通的，
> 但抖音评论里真实的动图评论一直没抓到 —— 用户提到的第二条作品
> （`7687969967506061823`）接口返回 `total: 1` 却 `comments: null`，
> 那条评论**接口就不返回**。等拿到真实样本再确认。

### 测试

新增 `tests/test_comment_image.py`（**53 项断言**），覆盖：抖音 / B 站
纯图评论不再被丢、图文评论正文正确、多图评论提全、坏结构不炸、
动图检测（多帧 GIF / 单帧 GIF / 静态图 / 不存在文件）、
节点排布（文字在上图片在下、多图截断、空评论不生成节点），
以及源码不变量（禁止 `Image.fromURL`、禁止 `Video.fromFileSystem`、
评论图必须走 `download_media_candidates`）。

其中**源码串检查要先剥注释**：`main.py` 顶部的 API 对照表里就写着
``Comp.Video.fromFileSystem``，朴素子串搜索会误报成「真的用了它」。

全量 19 个测试文件通过。

## v1.6.5（2026-09-20）

**性能：点歌列表图出图从 ~4.7 秒降到 ~0.6 秒（快 7 倍多）。**

### 问题

用户反馈「点歌拼图慢」。先量再改 —— 拆解 10 首歌 / 10 张封面的耗时：

| 环节 | 改前 | 改后 |
|---|---|---|
| 封面下载（10 张 `gather`） | 4778 ms | ~300 ms 起 |
| ├─ 其中 `resize(320→108, LANCZOS)` | 3380 ms / 10 张 | ~8 ms |
| ├─ PNG 编码（`optimize=True`） | 610 ms | 390 ms |
| **端到端（网易云）** | **4704 ms** | **610 ms** |
| **端到端（QQ音乐）** | **~1200 ms** | **679 ms** |

### 根因（三个独立瓶颈，都不在"算法"上）

1. **封面下的是原图，然后本地再缩一次，纯属浪费。**
   网易云的图片接口支持 `?param=宽y高` 直接返回指定尺寸（实测有效）。
   原图 545 ms / 3.9 KB vs 小图 204 ms / 2.8 KB；更亏的是紧接着的
   `resize(320→108, LANCZOS)` 在共享 CPU 上要 **338 ms/张**，
   而图已经是目标尺寸时 resize 是**空操作（8 ms）**。
   （QQ 音乐的封面本来就是 `R150x150`，不需要改。）

2. **解码是同步写在协程里的，把 event loop 堵死了 —— 这是最隐蔽的一处。**
   结果是 10 张封面的**并发下载变成了事实上的串行**：
   `gather` 4778 ms ≈ 串行 5198 ms，并发形同虚设。
   挪进 `asyncio.to_thread` 后并发才真正生效（Pillow 的 C 扩展会释放 GIL）。

3. **绘制 + PNG 编码也在 event loop 上。**
   800×1696 的 2 倍图，光 PNG 编码就 390~610 ms，这段时间里所有其它消息
   一起陪跑。整段挪进线程池。

### 修复

1. `_thumb_url()` —— 把封面 URL 改写成平台的小图地址（网易云加
   `?param=WyH`，尺寸跟着 `cover_size` 走；QQ音乐不动）。
2. `_download_cover()` —— 纯 IO 留在 event loop，解码丢 `asyncio.to_thread`。
3. `_decode_cover()` —— 抽出同步解码函数，加 `Image.draft()`
   让 JPEG 解码器直接按 1/2^n 缩放解码（实测 2.6x）；
   `LANCZOS` 换 `BILINEAR`（108px 的封面肉眼无差）。
4. `_draw_and_encode()` —— 绘制 + 编码整段抽出，由 `asyncio.to_thread` 调用。
5. **封面缓存**（`_cover_cache`，200 张上限 / FIFO 淘汰）：
   重复点同一首歌直接跳过网络。一张 108×108 RGB 约 35 KB，200 张 ≈ 7 MB。
6. 圆角遮罩 `_rounded_mask()` 加缓存 —— 一屏 10 张封面尺寸完全一样，
   之前每张都 `Image.new` + 画一次圆角矩形。
7. 去掉 `canvas.save(optimize=True)`：多花 170~320 ms 只省 1.5% 体积
   （199 KB → 202 KB），不划算。

### 顺带：README 补了「私信配置没反应」的排查指引

用户反馈私信发 `#R配置 cookie` 毫无回执。**根因不在插件** ——
AstrBot 的 `whitelist_check` 流水线阶段在事件**派发给插件之前**执行，
私信会话不在白名单里就直接 `stop_event()`，插件根本收不到这条消息。
「群里能用、私信不能用」正是这个缘故（群号在白名单里，私信会话 ID 是另一套）。
README 现在写了完整排查路径（怎么从日志认出来）和三种解法。

### 测试

新增 `tests/test_music_card_image_perf.py`（22 项断言），锁住：小图 URL 改写、
封面缓存命中时**不发请求**、解码尺寸语义，以及**三条最容易回退的源码不变量**
（解码必须走 `to_thread`、绘制必须走 `to_thread`、不许开 `optimize=True`）。
全量 18 个测试文件通过。

## v1.6.4（2026-09-20）

**改进：视频超过时长上限时，把作品链接一起发出来。**

### 问题

v1.6.3 让「超时长」这类拒绝至少会吭声了，但只回一句：

```
❌ 哔哩哔哩：视频时长 9分35秒 超过上限 8分0秒（可在插件配置里调整 biliDuration）
```

用户知道**为什么不发**了，却不知道**该去哪看** —— 还得自己拿标题去 B 站搜。

翻原版对超长视频的处理，三个平台其实**都只发文字、不给链接**：

| 平台 | 原版超时长时的行为 | 位置 |
|---|---|---|
| B站 | 发 `biliInfo`（标题 + 统计 + 简介 + 封面）+ 一段「限制说明」，然后 `return` | `tools.js:1667-1671` |
| 抖音 | 发「识别：抖音，作者 + 简介 + 封面」+「限制说明」 | `tools.js:891-905` |
| YouTube | 发「识别：油管，视频时长超限 + 标题」+ 缩略图 | `tools.js:3920-3924` |

也就是说原版**连个能点的链接都不给**（`constructBiliInfo` 的返回值里只有
标题 / 统计 / 简介 / 封面，没有 URL）。这里比原版再进一步。

### 修复

超时长的结果现在带上作品信息一起发：

```
🔗 哔哩哔哩
标题：东尼爆改麦晓雯！男人也可以这么美丽吗？！【bilibilionly同人扶持计划】
作者：流萤Zz
⏱️ 视频时长 9分35秒 超过上限 8分0秒，未下载（可在插件配置里调整 biliDuration）
👉 观看地址：https://www.bilibili.com/video/BV1awbW6cESg
```

实现：

1. `ResolveResult.reject()` 支持 `**kwargs`，把已经拿到的 `title` / `author` /
   `desc` / `extra` 一并带出（反正这些信息在时长检查之前就已经拿到了）。
2. `platforms/bilibili.py` 在 reject 时写入 `extra["web_url"]`（多 P 视频带 `?p=1`，
   和本插件「默认取第一 P」的行为对齐）。
3. `main.py` 的 `_render_text_only()` 支持渲染 `web_url`，
   并给按规则拒绝的原因行加 `⏱️` 前缀（和普通「备注」区分开）。
   rejected 分支现在也走这个渲染器，不再是干巴巴一句 `❌`。

### 为什么给的是作品页链接，不是 CDN 媒体直链

作品页链接（`https://www.bilibili.com/video/BVxxx`）稳定、点开就能看。
而 DASH 媒体直链（`upos-*.bilivideo.com/upgcxcode/...`）**带签名、几小时就过期**，
而且是**音视频分离**的两条轨，发出去过一会儿就是死链、手机上也播不了。

### 测试

`tests/test_reject_notice.py` 扩到 **36 项断言**，新增覆盖：reject 结果带出
标题 / UP主 / `web_url`、链接必须是作品页而非 `upgcxcode` 媒体直链、
以及 `_dispatch` 实际输出的文本里**确实包含可点的链接**。全量 17 个测试文件通过。

## v1.6.3（2026-09-20）

**修复：B 站视频超时长时不发也不说，用户完全不知道发生了什么。**

### 问题

用户在群里发了一条 `b23.tv` 作品链接，机器人**一声不吭**。翻服务器日志才看到：

```
[13:46:39.700] [WARN] 哔哩哔哩 解析失败: 视频时长 575s 超过配置上限 480s（可在配置里调整）
```

这条视频 9分35秒，超过了配置里的 8 分钟上限（`biliDuration`）。
**限制本身没问题**（那是用户自己配的），问题在于这类「解析出来了、但按规则不发」
的情况**和「网络抖动 / 接口报错」被混成了一类**，统一由 `plugin.reply_on_error`
控制，而该选项默认关闭 —— 于是用户看到的现象就是「发了链接，机器人没反应」，
**不去翻日志根本无从排查**。

更别扭的是：这类原因（时长上限）恰恰是**用户自己改个配置就能解决的**，
偏偏它最不吭声。

### 修复

1. **给解析结果加了「按规则拒绝」这个语义**（`ResolveResult.rejected` +
   `ResolveResult.reject()`）。它和普通失败的区分在于：

   | | `fail()` 真失败 | `reject()` 按规则拒绝 |
   |---|---|---|
   | 典型场景 | 接口超时、链接失效、Cookie 过期 | 时长超上限、功能未移植 |
   | 作品信息 | 没拿到 | **已经拿到了** |
   | 用户能否自救 | 一般不能，多半是后重试 | **能，改配置就行** |
   | 是否提示 | 看 `reply_on_error`（默认静默） | **始终提示** |

2. **B 站时长超限改用 `reject()`**，不受 `reply_on_error` 影响，一定回一句原因。

3. **时长文案改成给人看的**：以前的 `视频时长 575s 超过配置上限 480s`
   要用户自己换算，现在是
   `视频时长 9分35秒 超过上限 8分0秒（可在插件配置里调整 biliDuration）`。

4. **日志也分开了**：`reject` 的情况打「按规则未发送」，
   真失败才打「解析失败」，排查时一眼能分清。

### 没有改的（有意为之）

- **番剧（ep/ss）未移植**仍然是 `fail()` 而不是 `reject()`。区别在于：
  超时长用户改配置就能解决，而「功能没移植」用户自己改不了、只有开发者能处理，
  每次发番剧链接都回一条「未移植」只是噪音。
- **视频超体积**（`global.videoSizeLimit`，默认 70MB）本来就是无条件提示的
  （`_download_video` 里的 `MediaTooLarge` 分支），这次不需要动。
- **`reply_on_error` 的默认值没变**（仍是关闭）。网络类失败刷屏比静默更烦人。

### 测试

新增 `tests/test_reject_notice.py`（26 项断言），锁死三条不变量：

1. `reject()` / `fail()` / `ok()` 三者的 `rejected` 语义必须分得开；
2. B 站时长超限走的是 `reject()`，且文案是**可读时长**而非裸秒数；
3. **行为层面**：`rejected` 结果在 `reply_on_error=False` 时**仍然**会 yield 提示，
   而 `fail` 结果在该选项关着时保持静默（原行为不变）。

## v1.6.2（2026-09-20）

**修复：抖音图集总有那么一两张下载失败 / 发出来是糊的。**

### 问题

用户反馈「每次解析最好看的那张没了、下载失败」。抓服务器日志拿到了现场铁证：

```
[WARN] [R插件] 图片候选全部失败（2 个候选），末次错误:
       下载失败 HTTP 403: ...ooeEq6cACTnDqueQAAA96vfOA8EFpMEqwEmIJ9~tplv-dy-aweme-images:q75.jpeg
```

注意报错的是 **`.jpeg`（原图）候选**，而这张图的 `url_list` **只有 2 个候选**——
两个都被 403 之后，这张图就返回 `None`，**静默丢了**。

### 根因（实测数据，两条真实图集 / 7 张图 / 每张 3 轮探测）

抖音图集的 `url_list` 各候选有明确分工，**顺序不能照搬**：

| 候选位置 | 节点 | 后缀 | 实测结果 |
|---|---|---|---|
| `[0]` | `p3-pc-sign` | `.webp` | **可能 403**（每张图固定，不随机） |
| `[1]` | `p9-pc-sign` | `.webp` | 基本 200，但**是压缩预览** |
| `[2]` | `p3-pc-sign` | `.jpeg` | 基本 200，**是原图** |

`.webp` 走的是 `tplv-dy-aweme-images:q75`（质量 75 的压缩模板），实测同一张图
**107464 字节 vs 269965 字节，差一倍以上**。而之前是「按 `url_list` 顺序试到
第一个成功就用」，所以：

1. **经常拿到 `.webp` 缩略图** → 用户看到的图是糊的（「最好看的那张」变差了）
2. **候选少的图上直接丢图** → 两个候选都 403 就 `None`（上面的日志）

另外还发现一个更隐蔽的坑：抖音 CDN 拿不到图时**不返回 4xx/5xx**，而是回一个
`200` + `Content-Type: text/html` 的 **238 字节错误页**。旧代码只判断
`resp.status != 200`，会把这个 HTML **当成图片写盘**发出去（用户看到破图）。

### 修复

1. **图片候选改成「原图优先」排序**（`core/douyin_ssr.rank_image_candidates`）：
   `.jpeg/.jpg/.png/.heic/.avif` 排在 `.webp/.gif` 前面，同级内保持抖音原顺序
   （这样「换节点绕 403」的路仍然保留）。实测效果：

   | | 修复前（不排序） | 修复后（原图优先） |
   |---|---|---|
   | 图集 A 总量 | 425KB | **989KB**（+133%） |
   | 图集 B 总量 | 420KB | **597KB**（+42%） |

   图集 A 里 4 张从 `.webp` 换成了 `.jpeg`：104KB→263KB、55KB→177KB、
   77KB→217KB、62KB→206KB。

2. **下载器加「软失败」防护**（`core/downloader._stream_one(expect_media=True)`）：
   校验 `Content-Type` 必须是媒体类型、内容不能小于 1KB，否则当作失败换下一个候选。
   这样 403 错误页不会再被当成图片写盘。

3. **动图视频轨也走候选回退**（新增 `download_media_candidates`）：
   动图 `play_addr_h264.url_list` 有同样的问题，所有候选补齐进
   `extra["animated_video_candidates"]`，发送端逐个试。

### 保留的决定

**没有**改成原版那种 `segment.image(url)` 远程直发。原版看起来「全都成功」是因为
它**根本不下载**——把 URL 交给协议端抓，抓失败也不报错、不阻塞其它图，所以观感是
「全出来了」，但发出去的同样是 `.webp` 压缩图，失败的图在 QQ 里就是个空白框。
AstrBot 这边发送端不带 Referer、没有候选回退、不校验内容，直发会**整条消息链失败**
（详见 `_download_album_stills` 的说明），所以维持「一律先落盘」。

### 测试

- 新增 `tests/test_douyin_album_candidates.py`（20 项）：锁住「原图优先」排序、
  稳定排序不丢候选、真实候选重排后 jpeg 必须第一、动图带候选。
- 新增 `tests/test_downloader_media_guard.py`（9 项）：403 错误页必须被拦、
  过小响应必须被拦、坏了要能继续试下一个候选。
- 全量 16 个测试文件通过。

## v1.6.1（2026-09-19）

**修复：群里分享的「卡片」链接完全不解析。** 尤其是小红书。

### 问题

QQ 群里分享小红书 / B 站时，发出来的往往**不是链接文本，而是一张卡片**
（`CQ:json` 消息段）。这类消息有两个特点：

- `get_message_str()` 是**空串** → `@filter.regex` 的自动解析入口根本不响应
- 在机器人日志里只显示成 `[ComponentType.Json]`，看不出是哪家平台

之前只有 B 站小程序模块做了处理（`BiliMiniappFilter` 只认 B 站 appid），
其他平台的卡片一律**静默忽略**。用户在现场看到的现象是：
「22:40 我发了一条小红书的链接没解析出来」，而日志里连一行记录都没有。

### 修复

把卡片识别从「只认 B 站」改成**通用**：

- 从卡片原文里抠出所有 http(s) 链接，直接过一遍 `AUTO_RULES` 识别规则表
  —— 以后新增平台**不用再为卡片单独写判断**，只要域名在规则表里就能命中
- **不按字段名取链接**：实测两类卡片的字段位置完全不同，连 `app` 都不一样

  | 卡片类型 | 链接字段 | `app` |
  |---|---|---|
  | B 站小程序 | `meta.detail_1.qqdocurl` | `com.tencent.miniapp_01` |
  | 小红书图文分享 | `meta.news.jumpUrl` | `com.tencent.tuwen.lua` |

  所以只认「卡片里有没有我们认识的链接」，不认字段名、不认 appid。
- 卡片里的链接**常带 `\u0026` 转义**（`&` 的 JS 转义）。必须**先还原再抠
  链接** —— 反过来的话 URL 会从 `&` 处被截断，小红书的 `xsec_token`
  正好在 `&` 之后，一截断就变成「链接里缺少 xsec_token」。
- 顺手把卡片原文记进日志（**只记录、不发送**，且抹掉图片直链避免刷屏），
  以后再遇到新平台的卡片，照着日志补规则就行，不用对着手机截图猜字段。

### 实测

群 `878752629` 发一条小红书图文卡片 → 日志 `识别到 小红书 卡片` →
`小红书解析成功 type=normal 图片 4 张` → 卡片的 `xsec_token` /
`xsec_source` 完整带到解析请求，内容正常发出。

新增离线回归 `tests/test_card_link.py`（45 项），把真实抓到的卡片固化成
测试数据，锁住「不按字段名取链接」这个设计决定，以及 B 站小程序原有链路
不被改坏。

## v1.6.0（2026-09-18）

**解析内容可以打包成一条聊天记录（合并转发）发出，常用配置搬进聊天里的 `#R配置`。**
另外把小红书解析补上（上一版的源码里已修好，这次一并发布）。

### 新增：解析内容可打包成一条聊天记录

新增开关 `plugin.send_as_forward`（默认**关**，保持升级前行为）。打开后：

- 一次解析的**简介 + 全部图片 / 视频 / 音频**打包成**一条合并转发**发出来
- **不再单独发「识别成功」提示** —— 简介直接进第一个节点
- 一个媒体一个节点，顺序与作品一致；在 QQ 里每个媒体都是独立一条「消息」，
  可以单独转发 / 保存
- 整条解析**没有任何媒体**时不硬凑聊天记录：一条只有文字的转发点开跟普通消息
  一模一样还多一次点击，这种情况退回普通文字情报

聊天里可随时切换：

```
#R配置 形式 聊天记录          #R配置 形式 直发
```

合并转发里的媒体处理和直发**不是一回事**，这次踩到并修掉两点：

1. 转发节点里的 `Record` 会被 AstrBot 自己下载并转成 wav
   （`Node.to_dict` → `Record.convert_to_base64`），给它 URL 时这一步失败会抛异常，
   把**整条聊天记录**一起拖垮 —— 直发时它只是单独一条消息，代价完全不同。
   所以音频改为**先落盘再进节点**，失败只跳过这一个音频。
2. 图片同理一律先落盘再进节点；视频继续走 base64（`_video_component`）。

### 新增：`#R配置` —— 在聊天里改常用配置（管理员）

```
#R配置                          总览
#R配置 平台 抖音 开|关            开关某个平台的自动解析
#R配置 形式 聊天记录|直发          解析内容发送形式
#R配置 cookie 小红书              私信设置 Cookie（两步式）
#R配置 点歌 平台|数量|发送|方式|开关
```

- **必须带 `#` 或 `/`**：`R配置` 这几个字落到自然语言里的概率不低，裸关键词会误伤
- 平台名**中文名与内部 key 都认**（`抖音` / `douyin`、`小红书` / `xhs`）
- 每条命令都回一句「现在是什么」，改没改上一目了然

### 新增：Cookie 私信两步式 + 必备字段体检

Cookie 是凭据，**设置类操作只在私聊里生效**，群里执行只回提示、不写入：

```
#R配置 cookie 小红书    → 机器人提示后，把整串粘过来（3 分钟内有效）
```

**允许不完整，但必须带必备字段**：平台经常加新字段，要求填全只会把人挡在门外；
而一个必备字段都没有的串肯定是粘错了平台，直接拒收并点名缺什么
（小红书要 `web_session` 或 `a1`、B站要 `SESSDATA`、网易云要 `MUSIC_U`……）。
确实要强行写入可以加「强制」。

保存时会**顺手清空同平台的「逐项填写」** —— 后者优先级更高，留着旧值会把刚设置的
整串顶掉（B站扫码时踩过同一个坑）。

### 新增：小红书解析

- 短链展开 → 取 `xsec_token` → 解析页面里的 `__INITIAL_STATE__`
- **必须剔除 `webId`**：走网页 HTML 这条路，带它一定拿到验证页
  （实测 101903 字节），不带才是正常内容页（74874 字节）。
  「三件套缺一不可」的说法是走 API + x-s 签名的场景，网页端反而相反
- 视频流键已换成 `EF4 / EF5 / EF6 / EF7`（编码器代号），不再有 `stream.h264`；
  按「宽×高 → 码率」降序挑最优（`streamType` 数字与分辨率**不是**单调关系）
- 多画质档收进 `video_backups`，主地址下载失败自动换下一个
- 图文笔记与视频笔记都支持

### 改进

- **启动时汇总「已支持但没勾选」的平台**：「某平台链接发了没反应」九成是
  `plugin.enabled_platforms` 没勾上。以前只有真发了那条链接才会在日志里留一行
  跳过记录，事后排查很费劲；现在启动就说清楚，并提示用
  `#R配置 平台 <平台名> 开` 打开
- `#R菜单` 图片新增「管理员配置」区块，并显示当前发送形式与切换命令

### 测试

新增 `tests/test_forward_send.py`（20 项）、`tests/test_r_config.py`（60+ 项）。

`test_r_config.py` 里有一条值得留意的断言：用 AST 扫出所有
`self._save_conf("a.b", ...)` 的路径，逐个校验它们存在于 `_conf_schema.json` ——
AstrBot 会在插件代码执行前**按 schema 裁剪配置**，schema 里没有的键写进去当时
有效、下次启动就被删掉（v1.3.0 丢过一整套 Cookie）。以后新增可写配置项，
这条断言会直接把漏改 schema 的情况拦下来。

## v1.5.1（2026-09-17）

**修复「音频文件」模式可能发出坏文件**，并给所有音频直链加上发送前校验。

### 问题

网易云取直链（``player/url/v1``）拿不到音频时会走一个兜底：回落到
``song/media/outer/url?id=...``。而这个接口**已经废弃** —— 实测无论什么歌
（VIP / 免费 / 不存在的 id）都恒 302 到 ``music.163.com/404``，最终返回
``text/html``。

于是「音频文件」模式会发出一个名叫「歌名 - 歌手.mp3」、内容却是 404 网页的
文件 —— 用户下载后播不了。

### 修法

1. **去掉兜底**：``netease_play_url`` 拿不到就返回空串，上层退回
   「歌名 + 播放页链接」。那个常量也注释掉了，避免后人再加回来。
2. **发送前校验**：新增 ``verify_audio_url()``，用 ``Range: bytes=0-0`` 取
   一个字节，按「状态码 / Content-Type / 最终 URL」三重判断它是不是音频。
   卡片、语音条、音频文件三种发送方式都先过这一关，不通过就跳过并改发链接。
   结果带 10 分钟缓存（同一 URL 不重复校验）；网络异常时放行，不误杀。

### 测试

新增 ``tests/test_music_url_guard.py``（17 项）。

## v1.5.0（2026-09-17）

**新增网易云扫码登录 + 三个图片状态命令**（#R菜单 / #cookie状态 / #服务状态）。

### 新增：网易云扫码登录（`#RNQ`）

走网易云网页端官方扫码接口，**不需要自建 NeteaseCloudMusicApi**。
扫码确认后凭据自动写进 ``music.neteaseCookie``，并回显账号昵称与会员状态。

> 原 Guoba 面板带来的 `#rnq` 在 v1.3.0 被移除过（当时靠自建服务，没实现）。
> 这次用官方接口重新实现，所以恢复了。

### 新增：`#R菜单` —— 图片版功能菜单

展示所有指令与使用教程。菜单内容**从 `constants.py` 的规则表生成**，
不手写清单 —— 以后加平台/指令，菜单自动跟着变。

### 新增：`#cookie状态` —— 一图看清所有 Cookie

并行检查所有已配置的 Cookie，只展示**平台 + 账号昵称 + 状态**，
音乐平台额外给会员状态与等级；**不回显任何 Cookie 字段**。未配置的平台
合并成一行，保持简略。

输出示例（真实账号）：

    网易云音乐  幕Ming    黑胶VIP Lv.7　到期 2027-10-31
    QQ音乐      NaiLuo   豪华绿钻　Lv5
    抖音                 已配置（该平台暂无可用的校验接口）

抖音 / 快手 / 小红书 / 视频号没有可用的公开校验接口，标「已配置」而不是谎报「有效」。

### 新增：`#服务状态` —— 服务器负载 + Bot 信息（图片）

Bot 头像从 ``q1.qlogo.cn`` 拉、账号取 ``event.get_self_id()``、昵称尽量走
OneBot ``get_login_info``；负载用 psutil（镜像内置 7.1.3）取 CPU / 内存 /
磁盘 / 负载 / 运行时长，附运行环境（Python / Pillow / ffmpeg）。

### 图片渲染：随机背景 + 毛玻璃

新增 ``core/render_image.py``（基础设施）+ ``core/panels.py``（三张图）。

- 背景默认取 ``https://api.elaina.cat/random/``，可在配置里换地址，**留空则用纯色底**
- 毛玻璃底 + 半透明深色圆角面板，保证文字清晰
- **花体昵称自动折叠**：QQ 昵称常用 ``𝓝𝓪𝓲𝓛𝓾𝓸`` 这类数学变体字母，中文字体
  没有这些字形，直接画全是豆腐块；现在折回普通字母再渲染
- 外部请求（背景 / 头像 / Cookie 校验）全部并行发出，通常 1–3 秒出图

### 新增配置项

| 项 | 默认 | 说明 |
|---|---|---|
| ``plugin.panelBgApi`` | ``https://api.elaina.cat/random/`` | 图片命令的随机背景接口，留空用纯色底 |

### 测试

新增 ``tests/test_panels.py``。

## v1.4.2（2026-09-17）

**修掉序号点播可以无限重复触发的 bug。**

### 问题

用户反馈：「发了点歌后，已经发送序号获取了音乐，再发一次序号会重复再发一次」。

根因是 `_take_music_pick()` **只读不消费** —— 它会检查 TTL，但只在**过期时**
才删除会话。所以一次点歌之后，那个序号在 60 秒内可以反复发、每次都重新播一遍。

### 修法：拆成「读」与「消费」两步

```
_take_music_pick(event)    # 只读（peek），不过期就不删
_forget_music_pick(event)  # 显式消费，立刻删
```

`cmd_music_pick` 在**序号通过范围校验之后、任何 await 之前**同步调用
`_forget_music_pick()`：

- **点播成功** → 会话立刻清掉，再发同一个序号**静默无响应**
- **序号越界** → 只给提示、**不**清会话，用户可以直接改发别的序号

之所以要卡在「第一个 await 之前」：asyncio 是单线程事件循环，这段没有让出点，
所以「连点两次序号」产生的两个事件里，第二个拿到的必然是空会话 —— 不会重复发歌。

### 测试

新增 `tests/test_music_pick.py`（30 项）：

- 静态断言消费调用出现在第一个 `await` 之前；`_take_music_pick` 里的 `pop`
  只能有一处（且在有条件的 TTL 分支里）
- 用真实的 `Main.cmd_music_pick` 跑行为断言：首次点播后会话被清、再发无响应、
  越界不消费可重试、过期会话被清理、无会话时不拦截普通数字消息、
  不同会话互不干扰、开关关闭 / `searchMode=direct` 时不响应

## v1.4.1（2026-09-17）

**修掉 v1.4.0 引入的一个真 bug**：在「签名之后」改写卡片，会破坏签名，
导致网易云卡片发出去看不见。正解是**在请求侧指定平台**。

### 根因：`config.token` 是卡片内容的摘要，签名后不能改内容

实测对照（对 `ss.xingzhige.com` 同一 payload 连发）：

| 实验 | 结果 |
|---|---|
| 同一内容连发 5 次 | token **完全相同**（连跨秒都一致） |
| 只把 `title` 改一个字 | token 立刻不同，且两组 token **无交集** |

所以 v1.4.0 里「签名后按 jumpUrl 改写 `meta.music.tag`」的做法会让 token
与内容失配 —— QQ 直接不渲染这张卡。这也正好解释了当时的现象：
**QQ音乐的卡片正常，网易云的看不见**（QQ音乐那次的 tag 本就是「QQ音乐」，
没有触发改写，所以幸存）。

### 正解：平台品牌由**请求侧**的 `type` 决定

先确认官方 NapCat 对平台**零特殊处理** —— 它只是把参数原样转发给签名服务
（`packages/napcat-onebot/api/msg.ts:825`，完整函数已核对）：

    [OB11MessageDataType.music]: async ({ data }) => {
      const supportedPlatforms = ['qq', '163', 'kugou', 'kuwo', 'migu'];
      ...
      musicUrl = await RequestUtil.HttpGetJson<string>(signUrl, 'POST', postData);
      return ...json({ data: { data: musicJson } });
    }

而**卡片品牌（tag / appid / tagIcon）完全由请求里的 `type` 决定**
（实测，两个上游行为一致）：

| 请求 `type` | 返回 tag | appid | tagIcon |
|---|---|---|---|
| `custom` | `QQ音乐` | 100497308 | `p.qpic.cn/qqconnect/0/app_100497308_...` |
| `163` | **`网易云音乐`** | **100495085** | `i.gtimg.cn/open/app_icon/00/49/50/85/100495085_100_m.png` |

所以 `core/music_sign_proxy.py` 改成**在转发前**按歌曲页域名把 `type`
设成 `163`（网易云）/ `custom`（QQ音乐），并把 OneBot 的 `content`
映射成上游认的 `singer`。这些都是**签名之前**的参数，token 天然匹配。

服务器实测回读（`get_msg`）：

    网易云   tag='网易云音乐'  appid=100495085  token=a278b043...  ✅
    QQ音乐   tag='QQ音乐'      appid=100497308  token=b906dc3c...  ✅

### 附带澄清：id 模式已被上游彻底停用

用户以前用 NapCat 发网易云卡片会显示「网易云音乐」，那是签名服务支持
`{type:"163", id:<歌曲id>}` 的时代。现在两个上游都关了：

| 上游 | `163` + id |
|---|---|
| `ss.xingzhige.com` | HTTP 500「无法准确获取歌曲信息」 |
| `106.55.0.102:10087` | HTTP 400「缺少 title」 |

QQ 音乐的 id 模式同样（`ss.xingzhige.com` 返回纯文本「关闭id解析功能」、
yibai 返回 400）。也就是说 NapCat WebUI 里「主流平台 → 网易云音乐 → 音乐ID」
那条路径**现在也是坏的**，与本插件无关。本插件走 custom + 请求侧 `type`，
不依赖 id 模式。

### 测试

`tests/test_music_sign_proxy.py` 重写第 2 节，改为断言
`normalize_sign_request()` 的行为（域名 → type、content → singer、幂等、健壮性），
并**明确不再包含任何「改写已签名内容」的断言**。

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

### ~~修复：卡片平台标识张冠李戴~~（做法有误，v1.4.1 已重做）

> ⚠️ **本节描述的做法是错的，请看 v1.4.1。**
> 当时用「改写响应里的 tag / tagIcon」来纠正平台，但 `config.token` 是
> **卡片内容的摘要**，签名后改内容会让 token 失配、QQ 直接不渲染 ——
> 这反而弄坏了网易云卡片。正解是在**请求侧**指定 `type`。

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
