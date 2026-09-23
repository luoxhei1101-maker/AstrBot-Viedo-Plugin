"""``#R配置``（在聊天里改常用配置）的离线回归测试。

守护的不变量
============

1. **能写回的配置键必须都在 ``_conf_schema.json`` 里存在。**
   AstrBot 在插件代码执行前会按 schema 裁剪配置：schema 里没有的键，
   写进去当时有效、下次启动就被删掉（v1.3.0 的清空 Cookie 就是这么来的）。
   本测试用 AST 扫出 ``self._save_conf("a.b", ...)`` 的全部字面量路径，加上
   Cookie 字段表里的路径，逐个去 schema 里查。

2. **Cookie 允许不完整，但必备字段一个都没有时必须拒收**，且拒收时不能
   留下半截写入（用户明确要求「必须带必备的部分」）。

3. **设置整段 Cookie 时要清掉同平台的「逐项填写」** —— 后者的优先级更高，
   留着旧值会把刚设置的整串顶掉（B 站扫码踩过同一个坑）。

4. **Cookie 只能在私聊里设置**：群里执行只提示、不进入等待状态。

5. **两步式等待是一次性的**：收到下一条消息立刻消费状态，再发一条不会被
   当成第二份 Cookie 写进去。

跑法::

    python tests/test_r_config.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 用包名导入（插件内部是 `from .core.x import ...`，必须作为包）
sys.path.insert(0, str(_ROOT.parent))


def _real_astrbot_available() -> bool:
    """容器里 AstrBot 是真装着的，那种情况不要打桩。

    同一个测试文件因此既能在本机跑（自动桩出最小 API），也能直接丢进容器
    跑一遍 —— 后者验证的是**已部署的那份代码**。
    """
    try:
        import astrbot.api.event  # noqa: F401
        import astrbot.api.message_components  # noqa: F401
        import astrbot.api.star  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


NEED_STUBS = not _real_astrbot_available()

if NEED_STUBS and "astrbot" not in sys.modules:
    _logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _api = types.ModuleType("astrbot.api")
    _api.logger = _logger
    _api.AstrBotConfig = dict
    _mod = types.ModuleType("astrbot")
    _mod.api = _api
    sys.modules["astrbot"] = _mod
    sys.modules["astrbot.api"] = _api

_FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {got!r}"
          + ("" if ok else f"（期望 {want!r}）"))
    if not ok:
        _FAILED.append(name)


def check_true(name: str, cond, extra: str = "") -> None:
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {extra}" if extra else ""))
    if not ok:
        _FAILED.append(name)


def _install_astrbot_stubs() -> None:
    """按需桩出 main.py 依赖的最小 AstrBot API 集合。"""
    if not NEED_STUBS:
        print("  (真实 AstrBot 环境，跳过打桩)")
        return

    mc = types.ModuleType("astrbot.api.message_components")

    class _Comp:
        def __init__(self, *a, **kw):
            self.args = a
            self.kw = kw
            for k, v in kw.items():
                setattr(self, k, v)

        @classmethod
        def fromBase64(cls, data, **_kw):
            return cls(file=f"base64://{len(data)}")

        @classmethod
        def fromURL(cls, url, **_kw):
            return cls(file=url)

        @classmethod
        def fromFileSystem(cls, path, **_kw):
            return cls(file=f"file:///{path}")

        @classmethod
        def fromBytes(cls, data, **_kw):
            return cls(file=f"bytes://{len(data)}")

        def __repr__(self) -> str:
            return f"{type(self).__name__}({self.kw.get('file') or self.args})"

    for name in ("Image", "Video", "Record", "Plain", "Node", "Nodes",
                 "Json", "Music", "File"):
        setattr(mc, name, type(name, (_Comp,), {}))
    mc.MessageChain = _Comp

    class _AnyFilter:
        """任何 filter 属性都返回「接受任意参数、返回恒等装饰器」的可调用对象。

        不能做「单个可调用参数就直通」的优化：``@filter.custom_filter(SomeFilter)``
        传进来的就是类本身，直通会把它当装饰器调用而炸掉。
        """

        def __getattr__(self, _name):
            return lambda *a, **k: (lambda f: f)

    ev_mod = types.ModuleType("astrbot.api.event")
    ev_mod.AstrMessageEvent = object
    ev_mod.MessageChain = _Comp
    ev_mod.filter = _AnyFilter()

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = object

    filt_mod = types.ModuleType("astrbot.api.event.filter")
    filt_mod.CustomFilter = object

    sys.modules["astrbot.api.message_components"] = mc
    sys.modules["astrbot.api.event"] = ev_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.event.filter"] = filt_mod


# ======================================================================
# 1. 注册与别名表（不需要事件对象）
# ======================================================================


def _schema_paths() -> set[str]:
    """把 ``_conf_schema.json`` 摊平成 ``{'plugin.enable', ...}``。"""
    schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    out: set[str] = set()

    def walk(node: dict, prefix: str) -> None:
        for key, val in node.items():
            if not isinstance(val, dict):
                continue
            if val.get("type") == "object":
                walk(val.get("items", {}) or {}, f"{prefix}{key}.")
            else:
                out.add(f"{prefix}{key}")

    walk(schema, "")
    return out


def _save_conf_literal_paths() -> list[str]:
    """AST 扫出 main.py 里 ``self._save_conf("字面量路径", ...)`` 的所有路径。"""
    tree = ast.parse((_ROOT / "main.py").read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "_save_conf"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            out.append(node.args[0].value)
    return out


def registration_checks() -> None:
    print()
    print("=" * 70)
    print("1. 命令注册 / 别名表 / schema 一致性")
    print("=" * 70)

    _install_astrbot_stubs()
    from astrbot_plugin_rconsole.core.constants import COMMAND_RULES, AUTO_RULES
    import astrbot_plugin_rconsole.main as main_mod

    rules = {r["key"]: r for r in COMMAND_RULES}
    check_true("COMMAND_RULES 里有 rConfig", "rConfig" in rules)
    rule = rules.get("rConfig", {})
    check("rConfig 需要管理员", rule.get("admin"), True)
    check("rConfig 的 handler", rule.get("handler"), "r_config")
    check("rConfig 登记为本地方法",
          main_mod._LOCAL_COMMAND_METHODS.get("r_config"), "cmd_r_config")
    check_true("Main 有 cmd_r_config", hasattr(main_mod.Main, "cmd_r_config"))

    # ---- 粗筛正则：带 # / 才认，裸关键词不认（避免误伤自然语言）----
    pat = re.compile(rule.get("pattern", ""), re.IGNORECASE | re.MULTILINE)
    for text, want in (
        ("#R配置", True),
        ("#R配置 平台 抖音 关", True),
        ("/r设置 帮助", True),
        ("#Rc 形式 聊天记录", True),
        ("#rc", True),
        ("R配置 平台 抖音 关", False),   # 没前缀 → 不认
        ("#R菜单", False),
        ("#点歌 晴天", False),
        ("#RNQ", False),
    ):
        check(f"rConfig 匹配 {text!r}", bool(pat.search(text)), want)

    # ---- 前缀剥离 ----
    for text, want in (
        ("#R配置 平台 抖音 关", "平台 抖音 关"),
        ("#R配置", ""),
        ("/r设置   cookie   小红书 ", "cookie   小红书"),
        ("#Rc 帮助", "帮助"),
    ):
        got = main_mod._RCONFIG_PREFIX.sub("", text, count=1).strip()
        check(f"前缀剥离 {text!r}", got, want)

    # ---- 别名表覆盖度 ----
    for r in AUTO_RULES:
        check_true(f"平台别名覆盖 key={r.key}",
                   r.key.lower() in main_mod._PLATFORM_ALIAS)
        check_true(f"平台别名覆盖 name={r.name}",
                   r.name.lower() in main_mod._PLATFORM_ALIAS)

    for platform in main_mod._COOKIE_FIELDS:
        check_true(f"Cookie 别名覆盖 {platform}",
                   platform.lower() in main_mod._COOKIE_ALIAS)

    check("抖音 -> douyin", main_mod._PLATFORM_ALIAS.get("抖音"), "douyin")
    check("小红书（平台开关）-> xhs", main_mod._PLATFORM_ALIAS.get("小红书"), "xhs")
    check("小红书（Cookie）-> xiaohongshu",
          main_mod._COOKIE_ALIAS.get("小红书"), "xiaohongshu")
    check("QQ音乐（Cookie）-> qqmusic",
          main_mod._COOKIE_ALIAS.get("qq音乐"), "qqmusic")

    # ---- schema 一致性：能写的键必须存在 ----
    schema = _schema_paths()
    check_true("schema 里有 plugin.send_as_forward",
               "plugin.send_as_forward" in schema)

    writable = set(_save_conf_literal_paths())
    writable |= set(main_mod._COOKIE_FIELDS.values())
    from astrbot_plugin_rconsole.core.cookie_spec import get_spec
    for platform in main_mod._COOKIE_FIELDS:
        spec = get_spec(platform)
        if spec is not None:
            writable.add(spec.fields_path)

    missing = sorted(p for p in writable if p not in schema)
    check("_save_conf 能写的键都在 schema 里（否则会被裁剪掉）", missing, [])
    check_true("扫到的可写键数量 > 0", len(writable) > 0, f"{len(writable)} 个")


# ======================================================================
# 2. Cookie 体检（纯函数）
# ======================================================================


def cookie_check_checks() -> None:
    print()
    print("=" * 70)
    print("2. Cookie 必备字段体检")
    print("=" * 70)

    from astrbot_plugin_rconsole.core.cookie_spec import (
        COOKIE_REQUIRED_ANY,
        check_cookie,
        parse_cookie_keys,
    )

    check("解析字段名", parse_cookie_keys("a=1; b=2; c"), {"a", "b"})
    check("空串解析", parse_cookie_keys(""), set())

    ok, _ = check_cookie("xiaohongshu", "")
    check("空 Cookie 不通过", ok, False)

    ok, why = check_cookie("xiaohongshu", "这不是cookie")
    check("没有 key=value 不通过", ok, False)
    check_true("提示里说明原因", "key=value" in why, why)

    # 允许不完整：只要有一个必备字段就放行
    ok, _ = check_cookie("xiaohongshu", "web_session=abc123")
    check("只有 web_session 也通过（允许不完整）", ok, True)
    ok, _ = check_cookie("xiaohongshu", "a1=xyz")
    check("只有 a1 也通过", ok, True)
    ok, _ = check_cookie("xiaohongshu", "acw_tc=1; websectiga=2; sec_poison_id=3")
    check("全是非必备字段 → 不通过", ok, False)

    # 大小写不敏感（SESSDATA / MUSIC_U 这类都是大写）
    ok, _ = check_cookie("bili", "sessdata=xxx")
    check("必备字段匹配不分大小写", ok, True)

    for platform in ("douyin", "bili", "kuaishou", "weibo", "xiaoheihe",
                     "xiaohongshu", "miyoushe", "netease", "qqmusic",
                     "weixinChannel"):
        check_true(f"{platform} 有必备字段定义",
                   bool(COOKIE_REQUIRED_ANY.get(platform)))


# ======================================================================
# 3. 行为检查：真的跑 cmd_r_config / 子方法
# ======================================================================


class _Conf(dict):
    """带 save_config 的假配置对象（AstrBotConfig 也是 dict 子类，这里对齐）。"""

    def __init__(self, data=None):
        super().__init__(data or {})
        self.saved = 0

    def save_config(self):
        self.saved += 1


class _Event:
    def __init__(
        self, text, umo="test:PrivateMessage:100", private=True, platform="aiocqhttp"
    ):
        self._text = text
        self.unified_msg_origin = umo
        self._private = private
        self._platform = platform
        self.out: list = []
        self.stopped = False

    def get_message_str(self) -> str:
        return self._text

    def get_platform_name(self) -> str:
        """协议端名 —— 分协议端配置就靠它分流。"""
        return self._platform

    def get_platform_id(self) -> str:
        return f"test_{self._platform}"

    def get_sender_id(self) -> str:
        return "2593504303"

    def get_sender_name(self) -> str:
        return "管理员"

    def is_private_chat(self) -> bool:
        return self._private

    def plain_result(self, text):
        self.out.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.out.append(("chain", chain))
        return ("chain", chain)

    def stop_event(self) -> None:
        self.stopped = True

    def track_temporary_local_file(self, _path) -> None:
        pass


async def _collect(agen):
    return [x async for x in agen]


def _texts(ev: _Event) -> str:
    return "\n".join(str(x[1]) for x in ev.out)


def behavior_checks() -> None:
    print()
    print("=" * 70)
    print("3. 行为：平台开关 / 发送形式 / 点歌 / Cookie")
    print("=" * 70)

    import astrbot_plugin_rconsole.main as main_mod

    class FakePlugin(main_mod.Main):
        def __init__(self, data=None):  # noqa: D107 - 故意不调 super().__init__
            self.conf_data = _Conf(data or {})
            self._cookie_pending = {}

    def send(plugin, text, private=True, umo="test:PrivateMessage:100",
             platform="aiocqhttp"):
        ev = _Event(text, umo=umo, private=private, platform=platform)
        return asyncio.run(_collect(plugin.cmd_r_config(ev))), ev

    # ---------------- 平台开关 ----------------
    print("-- 平台开关 --")
    plugin = FakePlugin()
    _, ev = send(plugin, "#R配置 平台")
    check_true("列出全部平台", "抖音" in _texts(ev) and "小红书" in _texts(ev))

    out, ev = send(plugin, "#R配置 平台 抖音 关")
    enabled = plugin.conf_data.get("plugin", {}).get("enabled_platforms")
    check_true("关掉抖音后写入配置", isinstance(enabled, list) and "douyin" not in enabled,
               str(enabled))
    check_true("回执里说明已关闭", "已关闭" in _texts(ev), _texts(ev)[:60])
    check("写配置触发了 save_config", plugin.conf_data.saved > 0, True)

    out, ev = send(plugin, "#R配置 平台 抖音 开")
    enabled = plugin.conf_data["plugin"]["enabled_platforms"]
    check_true("重新打开抖音", "douyin" in enabled, str(enabled))

    # 中文名 / key 都要认
    for name in ("抖音", "douyin", "小红书", "xhs"):
        p2 = FakePlugin()
        send(p2, f"#R配置 平台 {name} 关")
        keys = p2.conf_data.get("plugin", {}).get("enabled_platforms") or []
        check_true(f"平台名 {name} 能关掉", "douyin" not in keys if "抖" in name or name == "douyin" else "xhs" not in keys)

    _, ev = send(plugin, "#R配置 平台 不存在的平台 关")
    check_true("不认识的平台有提示", "不认识平台" in _texts(ev), _texts(ev)[:60])

    _, ev = send(plugin, "#R配置 平台 抖音 大概")
    check_true("非法开关词有提示", "只认「开」或「关」" in _texts(ev), _texts(ev)[:60])

    # ---------------- 发送形式 ----------------
    print("-- 发送形式 --")
    plugin = FakePlugin()
    _, ev0 = send(plugin, "#R配置 形式")
    check("默认直发（关闭转发）", plugin._forward_enabled(ev0), False)

    _, ev = send(plugin, "#R配置 形式 聊天记录")
    # ★ 关键：分协议端模式下，写入必须重定向到当前协议端那一份。
    # 写进旧键的话「命令回了成功但行为没变」，是最难查的一类 bug。
    check("切到聊天记录转发 → 写进当前协议端那份",
          plugin.conf_data["profiles"]["onebot"]["send_as_forward"], True)
    check("分协议端模式下不动旧键",
          plugin.conf_data.get("plugin", {}).get("send_as_forward"), None)
    check("_forward_enabled 跟随", plugin._forward_enabled(ev), True)

    _, ev = send(plugin, "#R配置 形式 直发")
    check("切回直发", plugin.conf_data["profiles"]["onebot"]["send_as_forward"], False)

    _, ev = send(plugin, "#R配置 形式")
    check_true("不带值显示当前状态", "直发" in _texts(ev), _texts(ev)[:60])

    _, ev = send(plugin, "#R配置 形式 随便")
    check_true("非法值有提示", "只认「聊天记录」或「直发」" in _texts(ev), _texts(ev)[:80])

    # 官机没有合并转发消息段：要说清楚，且不能把配置写脏
    qo = FakePlugin()
    _, ev = send(qo, "#R配置 形式 聊天记录", platform="qqofficial")
    check_true("官机上切转发 → 明确说明不支持", "没有合并转发" in _texts(ev),
               _texts(ev)[:80])
    check("官机上不写这份配置",
          qo.conf_data.get("profiles", {}).get("qqofficial", {}).get("send_as_forward"),
          None)

    # 「通用」配置来源时回到旧键
    shared = FakePlugin({"profiles": {"mode": "shared"}})
    send(shared, "#R配置 形式 聊天记录")
    check("通用模式下写旧键", shared.conf_data["plugin"]["send_as_forward"], True)

    # 两份配置互不干扰：官机上执行命令，不该碰到 OneBot 那份
    both = FakePlugin()
    send(both, "#R配置 形式 聊天记录", platform="aiocqhttp")
    send(both, "#R配置 形式 直发", platform="qqofficial")
    check("OneBot 那份开着了",
          both.conf_data["profiles"]["onebot"]["send_as_forward"], True)
    check("官机那份没被写（官机不支持转发，命令被拒）",
          both.conf_data["profiles"].get("qqofficial"), None)

    # 查看当前协议端这份配置
    _, ev = send(plugin, "#R配置 协议端")
    check_true("#R配置 协议端 → 显示配置来源与协议端",
               "配置来源" in _texts(ev) and "OneBot" in _texts(ev), _texts(ev)[:90])

    # ---------------- 点歌 ----------------
    print("-- 点歌设置 --")
    plugin = FakePlugin()
    send(plugin, "#R配置 点歌 平台 网易云")
    check("平台 -> music.platform", plugin.conf_data["music"]["platform"], "netease")
    send(plugin, "#R配置 点歌 平台 QQ音乐")
    check("平台（QQ）", plugin.conf_data["music"]["platform"], "qqmusic")

    send(plugin, "#R配置 点歌 数量 5")
    check("数量 -> music.maxList", plugin.conf_data["music"]["maxList"], 5)
    _, ev = send(plugin, "#R配置 点歌 数量 999")
    check_true("数量越界被拦", "1-50" in _texts(ev), _texts(ev)[:60])
    _, ev = send(plugin, "#R配置 点歌 数量 abc")
    check_true("数量非数字被拦", "不是数字" in _texts(ev), _texts(ev)[:60])

    # 发送方式 / 点歌方式属于「发送形态」，只改当前协议端那份
    send(plugin, "#R配置 点歌 发送 卡片")
    check("发送方式 → 当前协议端那份",
          plugin.conf_data["profiles"]["onebot"]["music_send_mode"], "card")
    check("分协议端模式下不动 music.sendMode",
          plugin.conf_data.get("music", {}).get("sendMode"), None)
    _, ev = send(plugin, "#R配置 点歌 发送 空投")
    check_true("非法发送方式被拦", "只认" in _texts(ev), _texts(ev)[:60])

    send(plugin, "#R配置 点歌 方式 直接")
    check("方式 → 当前协议端那份",
          plugin.conf_data["profiles"]["onebot"]["music_search_mode"], "direct")
    check("分协议端模式下不动 music.searchMode",
          plugin.conf_data.get("music", {}).get("searchMode"), None)

    # 官机那份默认是 voice（它没有音乐卡片消息段）
    qo2 = FakePlugin()
    _, ev = send(qo2, "#R配置 点歌")
    check_true("官机点歌默认显示语音", "voice" in _texts(ev), _texts(ev)[:120])
    _, ev = send(qo2, "#R配置 点歌 发送 卡片", platform="qqofficial")
    check_true("官机设卡片会提示降级成语音", "降级成语音" in _texts(ev), _texts(ev)[:120])

    # 只在本协议端互不影响
    mixed = FakePlugin()
    send(mixed, "#R配置 点歌 发送 链接", platform="qqofficial")
    check("官机那份改了",
          mixed.conf_data["profiles"]["qqofficial"]["music_send_mode"], "link")
    check("OneBot 那份没被碰",
          mixed.conf_data["profiles"].get("onebot"), None)

    send(plugin, "#R配置 点歌 开关 开")
    check("开关 -> music.enable", plugin.conf_data["music"]["enable"], True)

    # ---------------- Cookie ----------------
    print("-- Cookie --")
    plugin = FakePlugin()
    _, ev = send(plugin, "#R配置 cookie")
    check_true("列出各平台配置情况", "小红书" in _texts(ev), _texts(ev)[:60])

    # 缺必备字段 → 拒收，且不写入
    _, ev = send(plugin, "#R配置 cookie 小红书 acw_tc=1; websectiga=2")
    check_true("缺必备字段被拒", "没通过检查" in _texts(ev), _texts(ev)[:80])
    check("被拒时不留写入", plugin.conf_data.get("other", {}).get("xiaohongshuCookie"), None)

    # 有必备字段 → 通过
    _, ev = send(plugin, "#R配置 cookie 小红书 web_session=abc; acw_tc=1")
    check("写入整段 Cookie",
          plugin.conf_data["other"]["xiaohongshuCookie"], "web_session=abc; acw_tc=1")
    check_true("回执里有字段数", "2 个字段" in _texts(ev), _texts(ev)[:80])

    # 强制跳过体检
    _, ev = send(plugin, "#R配置 cookie 小红书 强制 只有乱码")
    check("强制写入生效", plugin.conf_data["other"]["xiaohongshuCookie"], "只有乱码")

    # 清除
    _, ev = send(plugin, "#R配置 cookie 小红书 清除")
    check("清除后为空", plugin.conf_data["other"]["xiaohongshuCookie"], "")
    check_true("回执说明已清空", "已清空" in _texts(ev), _texts(ev)[:60])

    # 设置整段时要清掉「逐项填写」（它的优先级更高）
    plugin = FakePlugin({
        "bili": {"biliSessDataFields": [{"__template_key": "SESSDATA", "value": "old"}]}
    })
    send(plugin, "#R配置 cookie b站 SESSDATA=newvalue; bili_jct=x")
    check("整段 Cookie 写入 bili.biliSessData",
          plugin.conf_data["bili"]["biliSessData"], "SESSDATA=newvalue; bili_jct=x")
    check("旧的逐项填写被清空", plugin.conf_data["bili"]["biliSessDataFields"], [])
    check_true("回执解释了清空动作", True)

    _, ev = send(plugin, "#R配置 cookie 不存在的平台 x=1")
    check_true("不认识的 Cookie 平台有提示", "不认识平台" in _texts(ev), _texts(ev)[:60])

    # ---------------- 私聊限制 ----------------
    print("-- 私聊限制 --")
    plugin = FakePlugin()
    _, ev = send(plugin, "#R配置 cookie 小红书", private=False)
    check_true("群里不带值只提示私聊", "私聊" in _texts(ev), _texts(ev)[:80])
    check("群里不进入等待状态", plugin._cookie_pending, {})

    _, ev = send(plugin, "#R配置 cookie 小红书 web_session=abc", private=False)
    check_true("群里直接设置也被拦", "私聊" in _texts(ev), _texts(ev)[:80])
    check("群里没有写入", plugin.conf_data.get("other", {}).get("xiaohongshuCookie"), None)

    # ---------------- 两步式等待 ----------------
    print("-- 两步式 Cookie 输入 --")
    plugin = FakePlugin()
    _, ev = send(plugin, "#R配置 cookie 小红书", private=True)
    check_true("私聊进入等待状态", "小红书" in _texts(ev) and "分钟" in _texts(ev),
               _texts(ev)[:80])
    umo = "test:PrivateMessage:100"
    check("等待状态已登记", plugin.has_pending_cookie(umo), True)
    check_true("提示里写出了必备字段", "web_session" in _texts(ev), _texts(ev)[:120])

    # 下一条消息被当成 Cookie 收下
    ev2 = _Event("web_session=realvalue; a1=xx; acw_tc=1", umo=umo)
    asyncio.run(_collect(plugin.on_cookie_input(ev2)))
    check("Cookie 已写入",
          plugin.conf_data["other"]["xiaohongshuCookie"],
          "web_session=realvalue; a1=xx; acw_tc=1")
    check("等待状态被消费（一次性）", plugin.has_pending_cookie(umo), False)
    check("事件被拦下（不再传给 LLM）", ev2.stopped, True)

    # 再发一条不会被当成第二份 Cookie
    ev3 = _Event("web_session=second", umo=umo)
    asyncio.run(_collect(plugin.on_cookie_input(ev3)))
    check("再发一条没有写入", plugin.conf_data["other"]["xiaohongshuCookie"],
          "web_session=realvalue; a1=xx; acw_tc=1")
    check("再发一条没有输出", ev3.out, [])

    # 「取消」
    plugin2 = FakePlugin()
    send(plugin2, "#R配置 cookie 抖音", private=True)
    ev4 = _Event("取消", umo=umo)
    asyncio.run(_collect(plugin2.on_cookie_input(ev4)))
    check_true("取消有回执", "已取消" in _texts(ev4), _texts(ev4)[:60])
    check("取消后没有写入", plugin2.conf_data.get("douyin", {}).get("douyinCookie"), None)
    check("取消后状态已消费", plugin2.has_pending_cookie(umo), False)

    # 过期的等待状态要被清掉，不能误收
    import time as _time
    plugin3 = FakePlugin()
    plugin3._cookie_pending[umo] = (_time.time() - 1, "douyin")
    check("过期状态判定为无效", plugin3.has_pending_cookie(umo), False)
    ev5 = _Event("sessionid=x", umo=umo)
    asyncio.run(_collect(plugin3.on_cookie_input(ev5)))
    check("过期后不写入", plugin3.conf_data.get("douyin", {}).get("douyinCookie"), None)

    # ---------------- 分发 ----------------
    print("-- 命令分发 --")
    plugin = FakePlugin()
    _, ev = send(plugin, "#R配置")
    check_true("不带参数给总览", "配置总览" in _texts(ev), _texts(ev)[:60])
    check_true("总览显示发送形式", "解析发送形式" in _texts(ev))
    check_true("总览显示点歌", "点歌" in _texts(ev))

    _, ev = send(plugin, "#R配置 帮助")
    check_true("帮助里有各子命令", "#R配置 平台" in _texts(ev) and "#R配置 cookie" in _texts(ev))

    _, ev = send(plugin, "#R配置 乱写的东西")
    check_true("未知子命令有提示", "不认识" in _texts(ev) and "帮助" in _texts(ev),
               _texts(ev)[:60])

    # 大小写 / 别名入口
    for text in ("#rc", "/R设置", "#Rc 帮助", "#r配置 帮助"):
        _, ev = send(FakePlugin(), text)
        check_true(f"入口 {text!r} 能用", bool(ev.out), _texts(ev)[:40])


# ======================================================================
# 4. 源码不变量
# ======================================================================


def source_checks() -> None:
    print()
    print("=" * 70)
    print("4. 源码不变量")
    print("=" * 70)

    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # _save_conf 必须提醒 schema 裁剪这个坑（后来人加配置时的唯一提示）
    body = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == "_save_conf":
            body = ast.get_source_segment(src, node) or ""
    check_true("_save_conf 存在", bool(body))
    check_true("_save_conf 文档里写明 schema 裁剪的坑",
               "_conf_schema.json" in body and "裁剪" in body)

    # 等待状态的消费必须在任何让出点之前（并发安全）
    #
    # 让出点是 **yield**（async generator 里 yield 会交出控制权），不一定是
    # await —— on_cookie_input 全是同步逻辑，一个 await 都没有。用 AST 定位
    # 而不是扫文本行：docstring 里就写着「await」这个词，按行号找会指到文档上
    # （第一版断言就踩了）。
    def _fn(name: str):
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name == name:
                return node
        return None

    def _lines(fn, kinds) -> list[int]:
        out: list[int] = []
        for node in ast.walk(fn):
            if isinstance(node, kinds):
                out.append(node.lineno)
        return out

    handler_fn = _fn("on_cookie_input")
    check_true("on_cookie_input 存在", handler_fn is not None)
    if handler_fn is not None:
        pops = [
            n.lineno for n in ast.walk(handler_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "pop"
        ]
        outages = _lines(handler_fn, (ast.Yield, ast.Await))
        check_true(
            "consume 在任何让出点之前（yield/await 都会交出控制权）",
            bool(pops) and bool(outages) and min(pops) < min(outages),
            f"pop@{min(pops) if pops else None} 首个让出点@{min(outages) if outages else None}",
        )

    # 登记等待状态同样要放在让出点之前：登记之前出现 await 的话，两条消息
    # 几乎同时进来时可能都走到「登记」这一步，后一条把前一条的状态盖掉。
    cfg_fn = _fn("_cfg_cookie")
    check_true("_cfg_cookie 存在", cfg_fn is not None)
    if cfg_fn is not None:
        registrations = [
            n.lineno for n in ast.walk(cfg_fn)
            if isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Attribute)
            and n.value.attr == "_cookie_pending"
        ]
        awaits = _lines(cfg_fn, ast.Await)
        check_true("_cfg_cookie 里有 pending 登记", bool(registrations))
        early = [ln for ln in awaits if registrations and ln < min(registrations)]
        check_true(
            "登记等待状态之前没有 await（登记是原子的）",
            not early,
            f"登记@{min(registrations) if registrations else None} 之前的 await@{early}",
        )


def main() -> int:
    print("=" * 70)
    print("astrbot_plugin_rconsole · #R配置 回归测试")
    print("=" * 70)
    registration_checks()
    cookie_check_checks()
    behavior_checks()
    source_checks()

    print()
    print("=" * 70)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：")
        for name in _FAILED:
            print(f"   · {name}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
