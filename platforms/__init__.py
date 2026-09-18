"""平台 resolver 包。

导入这个包会把所有 resolver 注册进 ``base._REGISTRY``。
新增平台时：写好 resolver -> 在这里 import 一行 -> 在 ``core/constants.py``
的 ``AUTO_RULES`` 里加一条规则。
"""

from __future__ import annotations

from .base import ResolveResult, ResolverContext, call, get, names, not_ported, register

# 以下 import 有副作用（触发 @register），静态检查工具可能判定为「未使用」，
# 不能删。
from . import audio as _audio  # noqa: F401
from . import bilibili as _bilibili  # noqa: F401
from . import content as _content  # noqa: F401
from . import douyin as _douyin  # noqa: F401
from . import general as _general  # noqa: F401
from . import kuaishou as _kuaishou  # noqa: F401
from . import pending as _pending  # noqa: F401
from . import social as _social  # noqa: F401
from . import xiaohongshu as _xiaohongshu  # noqa: F401

__all__ = [
    "ResolveResult",
    "ResolverContext",
    "call",
    "get",
    "names",
    "not_ported",
    "register",
]
