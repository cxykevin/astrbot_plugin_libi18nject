"""astrbot_plugin_libi18nject 的进程级共享注册表。

其它插件（如 ``astrbot_plugin_librunner``）通过 :mod:`api` 往这里登记两类东西：

* **i18n 词条**：形如 ``{"zh-CN": {"features.config.aiSettings.runners.echo.title": "Echo"}}``。
  注入器会在 WebUI 的打包产物（locale chunk）送达浏览器**之前**把这些词条写进去，
  因此不需要修改磁盘上的 dashboard 文件。
* **纯文本 metadata section**：形如 ``{"echo_runner": {...}}``。内核的
  ``ConfigMetadataI18n.convert_to_i18n_keys()`` 会把 metadata 里的
  ``description`` / ``hint`` / ``labels`` 一律换成 i18n key（内核自带分组的词条在
  dashboard 静态语言文件里），注入方没有词条就会显示 ``[MISSING: ...]``；登记过的
  section 会在转换结果里被换回纯文本。

状态放在一个进程级共享模块上（而不是本模块的全局变量），这样 librunner 之类注入方
在被热重载、或本插件自己更新（模块被重新导入）时，注册内容依然有效。
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from collections.abc import Callable, Mapping
from typing import Any

from astrbot.api import logger

#: 插件名（必须与 metadata.yaml 中的 name、插件目录名一致）。
PLUGIN_NAME = "astrbot_plugin_libi18nject"

#: 进程级共享状态的载体模块名。刻意不带 ``data.plugins.`` 前缀，避免 AstrBot
#: 卸载/重载插件时清理 ``sys.modules`` 把状态一并丢掉。
_SHARED_MODULE_NAME = "astrbot_plugin_libi18nject_shared_state"

#: 通配 locale：登记到该键下的词条对所有语言生效（某语言没有专属词条时使用它）。
ANY_LOCALE = "*"

#: 「未传该参数」的哨兵（用于区分「不更新」与「更新为 None」）。
_UNSET: Any = object()


def _shared_state() -> dict[str, Any]:
    """返回进程级共享状态。

    Returns:
        ``{"entries": dict, "sections": dict, "listeners": list,
        "asset_patched": bool, "metadata_patched": bool, "patch_error": str | None}``
    """
    module = sys.modules.get(_SHARED_MODULE_NAME)
    if not isinstance(module, types.ModuleType):
        module = types.ModuleType(_SHARED_MODULE_NAME)
        sys.modules[_SHARED_MODULE_NAME] = module

    state = getattr(module, "state", None)
    if not isinstance(state, dict):
        state = {}
        module.state = state  # type: ignore[attr-defined]

    state.setdefault("entries", {})
    state.setdefault("sections", {})
    state.setdefault("listeners", [])
    state.setdefault("asset_patched", False)
    state.setdefault("metadata_patched", False)
    state.setdefault("patch_error", None)
    return state


def set_entries(owner: str, entries: Mapping[str, Mapping[str, str]]) -> None:
    """登记（或覆盖）某个 owner 的 i18n 词条。

    Args:
        owner: 注册方标识，通常是插件目录名。
        entries: ``{locale: {dotted_key: text}}``；locale 可以是
            :data:`ANY_LOCALE` 表示所有语言通用。
    """
    state = _shared_state()
    state["entries"][owner] = {
        locale: dict(table)
        for locale, table in entries.items()
        if isinstance(table, Mapping)
    }
    notify()


def drop_entries(owner: str) -> bool:
    """移除某个 owner 的 i18n 词条。

    Args:
        owner: 注册方标识。

    Returns:
        是否确实移除了内容。
    """
    state = _shared_state()
    if state["entries"].pop(owner, None) is None:
        return False
    notify()
    return True


def set_plain_sections(owner: str, getter: Callable[[], Mapping[str, Any]]) -> None:
    """登记某个 owner 需要保持纯文本的 metadata section。

    Args:
        owner: 注册方标识。
        getter: 无参可调用对象，返回 ``{section_key: section_dict}``，例如
            ``{"echo_runner": {"description": "Echo", "items": {...}}}``。每次
            metadata 转换时都会现取，因此注册方可以随时改变返回内容。
    """
    state = _shared_state()
    state["sections"][owner] = getter
    notify()


def drop_plain_sections(owner: str) -> bool:
    """移除某个 owner 的纯文本 section 登记。

    Args:
        owner: 注册方标识。

    Returns:
        是否确实移除了内容。
    """
    state = _shared_state()
    if state["sections"].pop(owner, None) is None:
        return False
    notify()
    return True


def merged_entries() -> dict[str, dict[str, str]]:
    """合并所有 owner 登记的 i18n 词条。

    Returns:
        ``{locale: {dotted_key: text}}``；同名 key 由 owner 名称排序决定胜者
        （保证结果稳定、可复现）。
    """
    state = _shared_state()
    merged: dict[str, dict[str, str]] = {}
    for owner in sorted(state["entries"]):
        table = state["entries"].get(owner) or {}
        for locale, entries in table.items():
            bucket = merged.setdefault(str(locale), {})
            for key, text in entries.items():
                bucket[str(key)] = str(text)
    return merged


def merged_plain_sections() -> dict[str, dict[str, Any]]:
    """收集所有 owner 登记的纯文本 section。

    Returns:
        ``{section_key: section_dict}``。单个 getter 抛错时跳过它，不影响其它
        owner（面板文案问题不应该阻断内核的 metadata 转换）。
    """
    state = _shared_state()
    merged: dict[str, dict[str, Any]] = {}
    for owner in sorted(state["sections"]):
        getter = state["sections"].get(owner)
        if not callable(getter):
            continue
        try:
            produced = getter()
        except Exception:  # noqa: BLE001 - 注册方问题不应影响内核
            logger.error(
                "libi18nject: 注册方 %s 的纯文本 section getter 执行失败", owner,
                exc_info=True,
            )
            continue
        if not isinstance(produced, Mapping):
            continue
        for key, value in produced.items():
            if isinstance(key, str) and isinstance(value, Mapping):
                merged[key] = value
    return merged


def entries_fingerprint() -> str:
    """返回当前词条的稳定指纹（用于缓存改写产物）。

    Returns:
        十六进制摘要；没有登记任何词条时返回 ``""``。
    """
    merged = merged_entries()
    if not merged:
        return ""
    payload = json.dumps(merged, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def notify() -> None:
    """通知监听者（注入器 / metadata 补丁）注册内容已变化。"""
    state = _shared_state()
    for listener in list(state["listeners"]):
        if not callable(listener):
            continue
        try:
            listener()
        except Exception:  # noqa: BLE001 - 单个监听者失败不影响注册流程
            logger.error("libi18nject: 注册表监听者执行失败", exc_info=True)


def add_listener(listener: Callable[[], None]) -> None:
    """订阅注册表变化事件。"""
    listeners = _shared_state()["listeners"]
    if listener not in listeners:
        listeners.append(listener)


def remove_listener(listener: Callable[[], None]) -> None:
    """取消订阅注册表变化事件。"""
    listeners = _shared_state()["listeners"]
    if listener in listeners:
        listeners.remove(listener)


def set_patch_state(
    *,
    asset_patched: bool | None = None,
    metadata_patched: bool | None = None,
    patch_error: str | None = _UNSET,
) -> None:
    """记录两部分补丁的当前状态（未传入的参数保持不变）。

    Args:
        asset_patched: assets 注入是否生效；None 表示不更新。
        metadata_patched: metadata 补丁是否生效；None 表示不更新。
        patch_error: 失败原因；不传表示不更新，传 None 表示清空。
    """
    state = _shared_state()
    if asset_patched is not None:
        state["asset_patched"] = bool(asset_patched)
    if metadata_patched is not None:
        state["metadata_patched"] = bool(metadata_patched)
    if patch_error is not _UNSET:
        state["patch_error"] = patch_error


def get_status() -> dict[str, Any]:
    """返回 libi18nject 的运行状态，供诊断使用。

    Returns:
        ``{"asset_patched": bool, "metadata_patched": bool, "patch_error": str | None,
        "owners": list[str], "locales": list[str], "entries": int,
        "plain_sections": list[str]}``
    """
    state = _shared_state()
    merged = merged_entries()
    return {
        "asset_patched": bool(state["asset_patched"]),
        "metadata_patched": bool(state["metadata_patched"]),
        "patch_error": state["patch_error"],
        "owners": sorted(set(state["entries"]) | set(state["sections"])),
        "locales": sorted(merged),
        "entries": sum(len(table) for table in merged.values()),
        "plain_sections": sorted(merged_plain_sections()),
    }
