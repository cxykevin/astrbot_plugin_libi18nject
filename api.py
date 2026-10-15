"""astrbot_plugin_libi18nject 的公共注入接口（供其它 AstrBot 插件调用）。

本插件负责 AstrBot WebUI 里「插件无法自己提供文案」的那部分 i18n：

1. **补 WebUI 打包产物的词条**（请求层改写 assets）：dashboard 的界面文案是打包进
   locale chunk 的，插件没有注入通道，缺少词条时会显示 ``[MISSING: …]``；
   本插件在静态资源送出去之前把词条写进对应语言包；
2. **保护纯文本 metadata 分组**：内核会把 metadata 的 ``description`` / ``hint`` /
   ``labels`` 无条件换成 i18n key，注入方没有词条就会显示 ``[MISSING: …]``；登记过的
   section 会在转换结果里保持纯文本。

用法（注入方插件）::

    from data.plugins.astrbot_plugin_libi18nject.api import (
        register_i18n_entries,
        register_plain_text_sections,
        unregister,
    )

    class MyPlugin(Star):
        async def initialize(self):
            register_i18n_entries(
                {
                    "zh-CN": {
                        "features.config.aiSettings.runners.myagent.title": "MyAgent",
                        "features.config.aiSettings.runners.myagent.summary": "当前由 MyAgent 处理 AI 对话。",
                        "features.config.aiSettings.runners.myagent.description": "连接 MyAgent 服务并配置访问参数。",
                    },
                    "en-US": {...},
                    "*": {...},  # 可选：其它语言没有专属词条时的兜底
                },
                owner="astrbot_plugin_myagent",
            )
            register_plain_text_sections(
                lambda: {"myagent_runner": {"description": "MyAgent", "items": {...}}},
                owner="astrbot_plugin_myagent",
            )

        async def terminate(self):
            unregister(owner="astrbot_plugin_myagent")

刻意的设计取舍：

* 注册内容放在进程级共享状态里（见 :mod:`registry`），注入方被热重载、或本插件自己
  更新时都不会丢；
* 注册是**覆盖式**的：同一个 owner 反复登记同一个键，以最后一次为准，方便注入方在
  每次注册表变化后整体重发；
* 只接受 :func:`supported_key_prefixes` 列出的前缀，拼错的键会被忽略并打印警告，
  不会静默产生「看起来注册了其实没生效」的假象。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import Any

from astrbot.api import logger

from . import registry
from .injector import ANCHORS

__all__ = [
    "ANY_LOCALE",
    "PLUGIN_NAME",
    "get_status",
    "register_i18n_entries",
    "register_plain_text_sections",
    "supported_key_prefixes",
    "unregister",
    "unregister_i18n_entries",
    "unregister_plain_text_sections",
]

#: 插件名（与 metadata.yaml / 目录名一致）。
PLUGIN_NAME = registry.PLUGIN_NAME

#: 通配 locale：登记到该键下的词条对所有语言生效。
ANY_LOCALE = registry.ANY_LOCALE


def supported_key_prefixes() -> tuple[str, ...]:
    """返回当前支持注入的词条前缀。

    Returns:
        前缀元组，例如 ``("features.config.aiSettings.runners.",)``。只有以这些前缀
        开头的 dotted key 才会被真正注入到 WebUI 打包产物里。
    """
    return tuple(anchor.prefix for anchor in ANCHORS)


def _caller_owner() -> str:
    """从调用栈推断注入方插件名。

    Returns:
        插件目录名（``data.plugins.<name>.xxx`` / ``astrbot.builtin_stars.<name>.xxx``）
        或子模块名；无法判断时返回 ``"unknown"``。
    """
    frame = sys._getframe(2)
    while frame is not None:
        module_name = frame.f_globals.get("__name__")
        if isinstance(module_name, str):
            parts = module_name.split(".")
            for flag in ("plugins", "builtin_stars"):
                if flag in parts:
                    index = parts.index(flag)
                    if index + 1 < len(parts):
                        return parts[index + 1]
            if parts and not parts[0].startswith("data") and parts[0] != __name__:
                return parts[0]
        frame = frame.f_back
    return "unknown"


def register_i18n_entries(
    entries: Mapping[str, Mapping[str, str]],
    *,
    owner: str | None = None,
) -> int:
    """登记（覆盖）某个 owner 的 WebUI 词条。

    Args:
        entries: ``{locale: {dotted_key: text}}``。locale 用 dashboard 的语言代码
            （``zh-CN`` / ``en-US`` / ``ja-JP`` / ``ru-RU``），或 :data:`ANY_LOCALE`
            （``"*"``）表示所有语言通用。
        owner: 注册方标识，默认从调用栈推断，建议显式传插件名。

    Returns:
        实际被接受（前缀合法）的词条数量。

    Raises:
        TypeError: ``entries`` 不是 ``{locale: {key: text}}`` 形式。
    """
    if not isinstance(entries, Mapping):
        raise TypeError("entries 必须是 {locale: {dotted_key: text}} 形式的字典")

    prefixes = supported_key_prefixes()
    accepted: dict[str, dict[str, str]] = {}
    count = 0
    for locale, table in entries.items():
        if not isinstance(table, Mapping):
            raise TypeError(f"entries[{locale!r}] 必须是 {{dotted_key: text}} 形式的字典")
        bucket: dict[str, str] = {}
        for key, text in table.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"entries[{locale!r}] 的键必须是非空字符串")
            if not any(key.startswith(prefix) for prefix in prefixes):
                logger.warning(
                    "libi18nject: 词条 %r 不在支持的范围内（可用前缀: %s），已忽略",
                    key,
                    "、".join(prefixes) or "（无）",
                )
                continue
            bucket[key] = str(text)
        accepted[str(locale)] = bucket
        count += len(bucket)

    owner = owner or _caller_owner()
    registry.set_entries(owner, accepted)
    logger.info(
        "libi18nject: 已登记 %s 的 %d 条 WebUI 词条（%d 种语言）",
        owner,
        count,
        len(accepted),
    )
    return count


def unregister_i18n_entries(*, owner: str | None = None) -> bool:
    """移除某个 owner 登记的 WebUI 词条。

    Args:
        owner: 注册方标识；为 None 时从调用栈推断。

    Returns:
        是否确实移除了内容。
    """
    return registry.drop_entries(owner or _caller_owner())


def register_plain_text_sections(
    getter: Callable[[], Mapping[str, Any]],
    *,
    owner: str | None = None,
) -> None:
    """登记某个 owner 需要保持纯文本的 metadata section。

    Args:
        getter: 无参可调用对象，返回 ``{section_key: section_dict}``，例如
            ``{"echo_runner": {"description": "Echo", "type": "object", "items": {...}}}``。
            每次内核转换 metadata 时都会现取，因此可以返回动态内容。
        owner: 注册方标识，默认从调用栈推断。

    Raises:
        TypeError: ``getter`` 不是可调用对象。
    """
    if not callable(getter):
        raise TypeError("getter 必须是返回 {section_key: section_dict} 的可调用对象")
    owner = owner or _caller_owner()
    registry.set_plain_sections(owner, getter)
    logger.info("libi18nject: 已登记 %s 的纯文本分组保护", owner)


def unregister_plain_text_sections(*, owner: str | None = None) -> bool:
    """移除某个 owner 的纯文本分组登记。

    Args:
        owner: 注册方标识；为 None 时从调用栈推断。

    Returns:
        是否确实移除了内容。
    """
    return registry.drop_plain_sections(owner or _caller_owner())


def unregister(*, owner: str | None = None) -> bool:
    """一次性移除某个 owner 的全部登记（词条 + 纯文本分组）。

    Args:
        owner: 注册方标识；为 None 时从调用栈推断。

    Returns:
        是否移除了任意内容。
    """
    target = owner or _caller_owner()
    removed = registry.drop_entries(target)
    removed = registry.drop_plain_sections(target) or removed
    return removed


def get_status() -> dict[str, Any]:
    """返回 libi18nject 的运行状态（供电台诊断使用）。

    Returns:
        见 :func:`registry.get_status`。
    """
    return registry.get_status()
