"""配置类型自愈。

**为什么需要这个模块**

AstrBot 的 ``check_config_integrity`` 只做一件事：按 schema 补齐**缺失**的键。
它**不会**把已经存在的值转成新类型。所以当插件升级、某个字段的 schema 类型变了
（比如 ``biliResolution`` 从 int 改成 string），老配置里留着的还是旧类型的值，
用户一在 WebUI 里点保存就报：

    格式校验未通过: ['错误的类型 bili.biliResolution: 期望是 string, 得到了 int']

而用户根本没改那个字段，纯粹是被历史数据坑了。

这个模块在插件加载时按 schema 逐个核对类型，发现不匹配**就地转换并落盘**，
让升级过程对用户无感。

只做「安全转换」：字符串化、数字字符串转数字、字符串转布尔。
拿不准的一律不动，宁可留着让用户自己改，也不擅自篡改配置。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

# schema type -> 期望的 Python 类型
_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "int": int,
    "float": float,
    "bool": bool,
    "string": str,
    "text": str,
    "list": list,
    "file": list,
    "template_list": list,
    "object": dict,
    "dict": dict,
}


def _is_correct(value: Any, schema_type: str) -> bool:
    """值是否已经符合 schema 声明的类型。"""
    expected = _TYPE_MAP.get(schema_type)
    if expected is None:
        return True

    if schema_type == "bool":
        return isinstance(value, bool)
    if schema_type in ("int", "float"):
        # bool 是 int 的子类，要排除掉
        return isinstance(value, expected) and not isinstance(value, bool)
    return isinstance(value, expected)


def _convert(value: Any, schema_type: str) -> tuple[bool, Any]:
    """尝试安全转换。返回 ``(是否转换了, 新值)``。"""
    if schema_type in ("string", "text"):
        # 数字/布尔 -> 字符串，总是安全的
        if isinstance(value, (int, float, bool)):
            return True, str(value)
        return False, value

    if schema_type == "int":
        if isinstance(value, str):
            text = value.strip()
            if text.lstrip("-").isdigit():
                return True, int(text)
        elif isinstance(value, float) and value.is_integer():
            return True, int(value)
        return False, value

    if schema_type == "float":
        if isinstance(value, str):
            try:
                return True, float(value.strip())
            except ValueError:
                return False, value
        if isinstance(value, int) and not isinstance(value, bool):
            return True, float(value)
        return False, value

    if schema_type == "bool":
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "1", "yes", "on"}:
                return True, True
            if text in {"false", "0", "no", "off"}:
                return True, False
        return False, value

    if schema_type in ("list", "file", "template_list"):
        # 标量包成单元素列表是安全的；反过来（列表退化成标量）不做
        if value in (None, ""):
            return True, []
        if not isinstance(value, list):
            return True, [value]
        return False, value

    if schema_type in ("object", "dict"):
        if value is None:
            return True, {}
        return False, value

    return False, value


def heal(conf: dict, schema: dict, *, save=None) -> list[str]:
    """按 schema 校对 conf 的每个叶子的类型，不匹配就转换。

    Args:
        conf: 插件配置（``AstrBotConfig`` 或普通 dict），**会被就地修改**。
        schema: ``_conf_schema.json`` 的内容。
        save: 可选的保存回调；发生了转换才会调用。

    Returns:
        变更记录，形如 ``["bili.biliResolution: 5(int) -> '5'"]``；无变更返回空列表。
    """
    changes: list[str] = []

    def walk(snode: dict, cnode: dict, path: str) -> None:
        for key, meta in snode.items():
            if not isinstance(meta, dict):
                continue
            p = f"{path}.{key}" if path else key
            stype = meta.get("type")

            if stype == "object":
                sub = cnode.get(key)
                if not isinstance(sub, dict):
                    if sub is None and key not in cnode:
                        continue
                    # 期望是嵌套对象但不是 -> 交给 _convert 兜成 {}
                    changed, new_val = _convert(sub, "object")
                    if changed:
                        cnode[key] = new_val
                        changes.append(f"{p}: {sub!r} -> {{}}")
                    sub = cnode.get(key)
                if isinstance(sub, dict):
                    walk(meta.get("items", {}), sub, p)
                continue

            if key not in cnode:
                continue

            value = cnode[key]
            if _is_correct(value, stype):
                continue

            changed, new_val = _convert(value, stype)
            if not changed:
                # 转不了就留着，不擅自改
                logger.debug(
                    f"[R插件][配置自愈] {p} 类型为 {type(value).__name__}，"
                    f"schema 期望 {stype}，无法安全转换，保持原样"
                )
                continue

            cnode[key] = new_val
            changes.append(
                f"{p}: {value!r}({type(value).__name__}) -> {new_val!r}"
            )

    if isinstance(conf, dict) and isinstance(schema, dict):
        walk(schema, conf, "")

    if changes and callable(save):
        try:
            save()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件][配置自愈] 保存失败: {type(exc).__name__}: {exc}")

    return changes


def migrate_cookie_fields(conf: dict) -> list[str]:
    """把旧版「dict」格式的 Cookie 逐项填写，迁到新版「template_list」格式。

    旧 schema 用 ``type: "dict"`` + ``template_schema``，存的是
    ``{"sessionid": "", "ttwid": ""}`` 这样的 dict；新 schema 改成
    ``type: "template_list"``，存的是
    ``[{"__template_key": "sessionid", "value": ""}]`` 这样的 list。

    这个迁移**必须**在通用 ``heal()`` 之前做：heal 对 ``template_list`` 的兜底
    是「把非 list 包成单元素 list」，会让 dict 变成 ``[{...}]``——里头的项没有
    ``__template_key``，反而把配置弄坏。

    Returns:
        迁移记录，形如 ``["douyin.douyinCookieFields: dict(12 键) -> template_list(3 项)"]``。
    """
    try:
        from .cookie_spec import COOKIE_SPECS
    except ImportError:  # pragma: no cover - 理论上 cookie_spec 一定在
        return []

    changes: list[str] = []
    for spec in COOKIE_SPECS:
        group = conf.get(spec.group)
        if not isinstance(group, dict):
            continue
        field = spec.fields_name
        old = group.get(field)
        if not isinstance(old, dict):
            # 已经是新格式（list），或者字段还不存在，跳过
            continue

        new_list: list[dict] = []
        for key, value in old.items():
            text = str(value or "").strip()
            if not text:
                continue
            new_list.append({"__template_key": key, "value": text})

        group[field] = new_list
        changes.append(
            f"{spec.group}.{field}: dict({len(old)} 键) -> template_list({len(new_list)} 项)"
        )

    return changes
