"""分协议端配置（``core/platform_profiles.py``）回归测试。

覆盖两类东西：

**1. 取值语义** —— 尤其是「升级不能改变老用户行为」那条回退规则。
   它靠「面板值是否等于默认值」判断用户改没改过，逻辑绕，最容易在重构时写坏。

**2. schema 一致性（最重要）** —— AstrBot 会在插件代码执行前按
   ``_conf_schema.json`` 裁剪配置，schema 里没有的键会被**静默删掉**。
   所以「代码字段」和「schema 键」必须一一对应，这里把两边锁死：
   以后加字段只改一边，这条测试立刻红。

跑法（要在装了依赖的虚拟环境里）::

    python tests/test_platform_profiles.py
"""

from __future__ import annotations

import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from astrbot_plugin_rconsole.core import platform_profiles as pp  # noqa: E402

FAILED: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}\n       期望: {want!r}\n       实际: {got!r}")
        FAILED.append(label)


def check_true(label: str, cond, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}" + (f"\n       {detail}" if detail else ""))
        FAILED.append(label)


def _get(d: dict, key: str, default=None):
    node = d
    for part in key.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
    return node


def _schema_conf(schema: dict, shared_path: str):
    """把旧键路径映射到 schema 里的位置。

    schema 的结构是「顶层分组 → items → 配置项」，所以
    ``plugin.send_mode`` 在 JSON 里是 ``plugin.items.send_mode``。
    """
    group, _, leaf = shared_path.partition(".")
    return _get(schema, f"{group}.items.{leaf}")


# ==========================================================================
# A) 分组
# ==========================================================================

def part_a_group() -> None:
    print("\n[A] 协议端 → 配置分组")
    check("aiocqhttp → onebot", pp.group_for("aiocqhttp"), pp.ONEBOT)
    check("QQOFFICIAL 大写也认", pp.group_for("QQOfficial"), pp.QQOFFICIAL)
    check("qqofficial_webhook → 官机", pp.group_for("qqofficial_webhook"), pp.QQOFFICIAL)
    check("qq_official 下划线写法 → 官机", pp.group_for("qq_official"), pp.QQOFFICIAL)
    check("wechatpadpro → 兜底", pp.group_for("wechatpadpro"), pp.FALLBACK)
    check("空 → 兜底", pp.group_for(""), pp.FALLBACK)
    check("None → 兜底", pp.group_for(None), pp.FALLBACK)

    check_true("三个分组都有定义",
               set(pp.GROUP_FIELDS) == {pp.ONEBOT, pp.QQOFFICIAL, pp.FALLBACK})
    # 专属字段只出现在该出现的组里
    onebot_keys = {f.key for f in pp.GROUP_FIELDS[pp.ONEBOT]}
    qo_keys = {f.key for f in pp.GROUP_FIELDS[pp.QQOFFICIAL]}
    fb_keys = {f.key for f in pp.GROUP_FIELDS[pp.FALLBACK]}
    check_true("合并转发只在 OneBot 组（官机没这个消息段）",
               "send_as_forward" in onebot_keys and "send_as_forward" not in qo_keys)
    check_true("图集超限合并也只在 OneBot 组",
               "album_forward_when_exceed" in onebot_keys
               and "album_forward_when_exceed" not in qo_keys)
    check_true("签名代理只在 OneBot 组",
               "enable_sign_proxy" in onebot_keys
               and "enable_sign_proxy" not in qo_keys)
    check_true("MD 图宽只在官机组",
               "md_image_width" in qo_keys and "md_image_width" not in onebot_keys)
    check_true("消息按钮只在官机组",
               "qq_buttons" in qo_keys and "qq_buttons" not in onebot_keys)
    check_true("兜底组只放通用项", "md_image_width" not in fb_keys
               and "enable_sign_proxy" not in fb_keys)


# ==========================================================================
# B) 模式
# ==========================================================================

def part_b_mode() -> None:
    print("\n[B] 配置来源")
    check("空 → 分协议端", pp.mode({}), pp.MODE_PER_PLATFORM)
    check("None → 分协议端", pp.mode(None), pp.MODE_PER_PLATFORM)
    check("显式 shared", pp.mode({"mode": "shared"}), pp.MODE_SHARED)
    check("大小写 / 空格容错", pp.mode({"mode": " SHARED "}), pp.MODE_SHARED)
    check("不认识的模式回落到分协议端",
          pp.mode({"mode": "whatever"}), pp.MODE_PER_PLATFORM)
    check_true("is_shared", pp.is_shared({"mode": "shared"})
               and not pp.is_shared({}))


# ==========================================================================
# C) 取值语义
# ==========================================================================

def part_c_value() -> None:
    print("\n[C] 取值语义（含升级兼容回退）")

    def getter(data):
        return lambda path, default=None: _get(data, path, default)

    # ---- C1 组默认 ----
    check("官机默认不转发",
          pp.value({}, "qqofficial", "send_as_forward", None), False)
    check("官机默认点歌用语音",
          pp.value({}, "qqofficial", "music_send_mode", None), "voice")
    check("OneBot 默认点歌用链接",
          pp.value({}, "aiocqhttp", "music_send_mode", None), "link")
    check("OneBot 默认可以转发",
          pp.value({}, "aiocqhttp", "album_forward_when_exceed", None), True)
    check("官机默认不合并图集",
          pp.value({}, "qqofficial", "album_forward_when_exceed", None), False)
    check("官机下载并发更保守",
          pp.value({}, "qqofficial", "download_concurrency", None), 4)
    check("兜底组不发合并转发",
          pp.value({}, "telegram", "album_forward_when_exceed", None), False)

    # ---- C2 面板显式设置优先 ----
    prof = {"qqofficial": {"music_send_mode": "link"}}
    check("面板里改过 → 用面板值",
          pp.value(prof, "qqofficial", "music_send_mode", None), "link")

    # ★ C3 关键坑：False / 0 是**有效配置**，不能被当成「没设」
    prof = {"qqofficial": {"send_as_forward": False}}
    check("面板里的 False 生效（不是「没设」）",
          pp.value(prof, "qqofficial", "send_as_forward", None), False)
    prof = {"onebot": {"send_as_forward": False}}
    check("OneBot 面板显式关掉转发",
          pp.value(prof, "aiocqhttp", "send_as_forward", None), False)

    # ---- C4 shared 模式读旧键 ----
    shared = {"mode": "shared"}
    old = {"plugin": {"send_as_forward": True, "max_images": 3}}
    check("shared 模式读旧键（bool）",
          pp.value(shared, "aiocqhttp", "send_as_forward", getter(old)), True)
    check("shared 模式读旧键（int）",
          pp.value(shared, "aiocqhttp", "max_images", getter(old)), 3)
    check("shared 模式下旧键没有 → 用组默认",
          pp.value(shared, "aiocqhttp", "max_videos", getter(old)), 9)

    # ---- C5 ★ 升级兼容：面板是默认值，但旧键被用户改过 ----
    #
    # 这是最容易写坏的一条。没有它，老用户调好的 plugin.send_as_forward=True
    # 升级后会被静默忽略 —— 表现是「我明明开了合并转发，怎么不发了」。
    old_changed = {"plugin": {"send_as_forward": True}}
    check("面板默认 + 旧键改过 → 继承旧键（升级不改变行为）",
          pp.value({}, "aiocqhttp", "send_as_forward", getter(old_changed)), True)

    old_changed_int = {"plugin": {"max_images": 2}}
    check("int 同理",
          pp.value({}, "aiocqhttp", "max_images", getter(old_changed_int)), 2)

    old_music = {"music": {"sendMode": "card"}}
    check("点歌发送方式同理",
          pp.value({}, "aiocqhttp", "music_send_mode", getter(old_music)), "card")

    # 旧键没被改过（等于 schema 默认）→ 不应该回退，用组默认
    old_default = {"plugin": {"send_as_forward": False, "max_images": 9}}
    check("旧键等于默认值 → 不回退，用组默认（官机点歌=语音）",
          pp.value({}, "qqofficial", "music_send_mode", getter(old_default)), "voice")

    # 面板改过时，旧键不再参与
    prof = {"onebot": {"max_images": 4}}
    check("面板改过 → 面板优先于旧键",
          pp.value(prof, "aiocqhttp", "max_images", getter(old_changed_int)), 4)

    # ---- C6 类型容错 ----
    check("字符串 'true' → True",
          pp.value({"onebot": {"send_as_forward": "true"}}, "aiocqhttp",
                   "send_as_forward", None), True)
    check("字符串 '3' → 3",
          pp.value({"onebot": {"max_images": "3"}}, "aiocqhttp",
                   "max_images", None), 3)
    check("坏值 → 组默认",
          pp.value({"onebot": {"max_images": "abc"}}, "aiocqhttp",
                   "max_images", None), 9)
    check("未知字段 → default",
          pp.value({}, "aiocqhttp", "nonexistent_field", None, "X"), "X")


# ==========================================================================
# D) 写入重定向
# ==========================================================================

def part_d_save_path() -> None:
    print("\n[D] 写入路径重定向（#R配置 改配置时用）")
    check("OneBot：旧的 form 键 → onebot 组",
          pp.save_path({}, "aiocqhttp", "plugin.send_as_forward"),
          "profiles.onebot.send_as_forward")
    check("官机：同上 → 官机组",
          pp.save_path({}, "qqofficial", "plugin.send_as_forward"),
          "profiles.qqofficial.send_as_forward")
    check("点歌发送方式也重定向",
          pp.save_path({}, "aiocqhttp", "music.sendMode"),
          "profiles.onebot.music_send_mode")
    check("shared 模式 → 原样写旧键",
          pp.save_path({"mode": "shared"}, "aiocqhttp", "plugin.send_as_forward"),
          "plugin.send_as_forward")
    # 不属于发送形态的路径不该被动
    check("Cookie 路径原样",
          pp.save_path({}, "aiocqhttp", "other.xiaohongshuCookie"),
          "other.xiaohongshuCookie")
    check("平台开关原样",
          pp.save_path({}, "qqofficial", "plugin.enabled_platforms"),
          "plugin.enabled_platforms")
    check("点歌总开关原样",
          pp.save_path({}, "qqofficial", "music.enable"),
          "music.enable")
    check("图集上限重定向",
          pp.save_path({}, "qqofficial", "plugin.max_images"),
          "profiles.qqofficial.max_images")
    check("MD 图宽重定向",
          pp.save_path({}, "qqofficial", "plugin.mdImageWidth"),
          "profiles.qqofficial.md_image_width")


# ==========================================================================
# E) ★ schema 一致性
# ==========================================================================

def part_e_schema() -> None:
    print("\n[E] schema 一致性（少了键 AstrBot 会静默删配置）")
    schema_path = _ROOT / "_conf_schema.json"
    check_true("有 _conf_schema.json", schema_path.exists())
    if not schema_path.exists():
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    items = _get(schema, "profiles.items")
    check_true("schema 里有 profiles 分组", isinstance(items, dict))
    if not isinstance(items, dict):
        return

    # ---- mode ----
    check_true("profiles.mode 在 schema 里", isinstance(items.get("mode"), dict))
    check("mode 默认值 = 分协议端",
          _get(schema, "profiles.items.mode.default"), pp.MODE_PER_PLATFORM)
    check("mode 的 options 与代码一致",
          _get(schema, "profiles.items.mode.options"), list(pp.MODE_OPTIONS))

    # ---- 每个分组的每个字段 ----
    for group, fields in pp.GROUP_FIELDS.items():
        node = _get(schema, f"profiles.items.{group}.items") or {}
        check_true(f"schema 有 profiles.{group} 分组", bool(node))
        missing = [f.key for f in fields if f.key not in node]
        check_true(f"{group} 组字段齐全（代码 {len(fields)} / schema {len(node)}）",
                   not missing, f"缺: {missing}")
        extra = [k for k in node if k not in {f.key for f in fields}]
        check_true(f"{group} 组没有多余键", not extra, f"多: {extra}")

    # ---- 默认值必须与代码一致 ----
    #
    # 不一致的危害：面板显示一个值、插件实际用另一个值，用户怎么调都调不对。
    for group, fields in pp.GROUP_FIELDS.items():
        for field in fields:
            want = pp.defaults(group)[field.key]
            got = _get(schema, f"profiles.items.{group}.items.{field.key}.default")
            check(f"默认值一致：{group}.{field.key}", got, want)

    # ---- 旧键必须保留（shared 模式的数据来源）----
    for field in pp.FIELDS:
        got = _schema_conf(schema, field.shared)
        check_true(f"旧键保留：{field.shared}", got is not None)

    # ---- SHARED_DEFAULTS 必须与 schema 旧键默认一致 ----
    #
    # 不一致的后果：判断「用户改没改过旧键」会失准 —— 老用户明明没动过，
    # 却被判成改过，于是组默认值永远不生效。
    for path, want in pp.SHARED_DEFAULTS.items():
        node = _schema_conf(schema, path)
        got = node.get("default") if isinstance(node, dict) else None
        check(f"SHARED_DEFAULTS 与 schema 一致：{path}", got, want)

    # ---- 反向：schema 里的字段代码都认识 ----
    for group in pp.GROUP_FIELDS:
        node = _get(schema, f"profiles.items.{group}.items") or {}
        for key in node:
            check_true(f"schema 里的 {group}.{key} 代码认识",
                       key in pp._FIELD_BY_KEY)


# ==========================================================================
# F) main.py 接线
# ==========================================================================

def part_f_main() -> None:
    print("\n[F] main.py 接线")
    src = (_ROOT / "main.py").read_text(encoding="utf-8")

    check_true("有 conf_plat 方法", "def conf_plat(" in src)
    check_true("_platform_profiles 读的是 profiles 分组",
               'self.conf_get("profiles", None)' in src)
    check_true("conf_plat 走 profile_value",
               "profile_value(" in src and "self.conf_get," in src)
    check_true("_save_conf 支持按协议端重定向",
               "profile_save_path(" in src)
    check_true("送 forma 时带了 event（否则写入会落错地方）",
               'self._save_conf("plugin.send_as_forward", True, event)' in src)
    check_true("点歌发送方式写入也带 event",
               'self._save_conf("music.sendMode", value, event)' in src)

    # 曾经踩过的坑：schema 里不存在的键读出来永远是空
    check_true("不再依赖已死的 plugin.platformProfiles",
               'conf_get("plugin.platformProfiles"' not in src)

    # 发送链路上的关键取值都改成了按协议端
    for key in ("send_mode", "max_images", "send_as_forward",
                "album_forward_when_exceed", "show_desc", "only_group",
                "reply_on_error", "download_concurrency", "max_videos",
                "md_image_width", "qq_buttons", "music_send_mode",
                "music_search_mode"):
        check_true(f'conf_plat 覆盖了 {key}',
                   f'conf_plat(event, "{key}"' in src)

    # 签名代理是启动时起的常驻服务，必须固定读 onebot 组
    check_true("签名代理固定读 onebot 组",
               "def _sign_proxy_enabled" in src
               and '"aiocqhttp", "enable_sign_proxy"' in src)

    import ast as _ast
    try:
        _ast.parse(src)
        check_true("main.py 语法 OK", True)
    except SyntaxError as exc:
        check_true("main.py 语法 OK", False, str(exc))


def part_g_cap_fallback() -> None:
    """★ 能力兜底：配置选的能力本协议端没有 → 降级。

    为什么必须测：``value()`` 有一条「面板还是默认值时继承旧键」的兼容规则，
    而老用户很可能把 ``music.sendMode`` 设成了 ``card``（音乐卡片）。
    这个值会被带到**官机那份配置**里 —— 但官机没有 music 消息段，
    硬发就是整条消息失败。所以 ``_music_send_mode`` 必须按能力再兜一层。

    规则：**偏好不能盖过能力**。
    """
    print("\n[G] 能力兜底（偏好不能盖过能力）")
    import astrbot_plugin_rconsole.main as main_mod

    class _Conf(dict):
        def save_config(self):
            pass

    class _Ev:
        def __init__(self, platform):
            self._platform = platform
            self.unified_msg_origin = "test:GroupMessage:1"

        def get_platform_name(self):
            return self._platform

        def get_platform_id(self):
            return f"t_{self._platform}"

        def get_message_str(self):
            return ""

    class P(main_mod.Main):
        def __init__(self, data):  # noqa: D107
            self.conf_data = _Conf(data)

    # 用户旧配置里写的是 card（音乐卡片）
    legacy = P({"music": {"sendMode": "card"}})
    check("官机：card 降级成 voice（它没有音乐卡片消息段）",
          legacy._music_send_mode(_Ev("qqofficial")), "voice")
    check("OneBot：card 保持（它支持音乐卡片）",
          legacy._music_send_mode(_Ev("aiocqhttp")), "card")

    # 面板里显式给官机设成 card 也一样降级
    panel = P({"profiles": {"qqofficial": {"music_send_mode": "card"}}})
    check("面板显式设 card 在官机上也降级",
          panel._music_send_mode(_Ev("qqofficial")), "voice")

    # 认不出的值走最稳的 link
    bogus = P({"profiles": {"onebot": {"music_send_mode": "空投"}}})
    check("认不出的值 → link",
          bogus._music_send_mode(_Ev("aiocqhttp")), "link")

    # 官机的语音默认值不会被误降级
    plain = P({})
    check("官机默认就是 voice",
          plain._music_send_mode(_Ev("qqofficial")), "voice")

    # 合并转发同理：官机配了也发不出去，必须被能力挡住
    forward = P({"profiles": {"qqofficial": {"send_as_forward": True}}})
    check("官机上合并转发被能力挡住（配了也不发）",
          forward._forward_enabled(_Ev("qqofficial")), False)
    check("官机那份的值不会泄漏到 OneBot（两份是隔离的）",
          forward._forward_enabled(_Ev("aiocqhttp")), False)

    # OneBot 那份配了就是配了
    on_ob = P({"profiles": {"onebot": {"send_as_forward": True}}})
    check("OneBot 上开启合并转发生效",
          on_ob._forward_enabled(_Ev("aiocqhttp")), True)
    check("同一个实例走官机时不受 OneBot 那份影响",
          on_ob._forward_enabled(_Ev("qqofficial")), False)


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · 分协议端配置回归测试")
    print("=" * 70)
    part_a_group()
    part_b_mode()
    part_c_value()
    part_d_save_path()
    part_e_schema()
    part_f_main()
    part_g_cap_fallback()

    print()
    print("=" * 70)
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败：")
        for item in FAILED:
            print("   -", item)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
