"""在 HTTP 请求层面改写 WebUI 打包产物（assets）的注入器。

**它解决什么问题**：dashboard 的前端文案（例如「更换执行方式」对话框里的
``aiSettings.runners.<type>.title``）全部打包在 locale chunk 里，插件侧原本没有任何
注入通道。libi18nject 的做法是**不动磁盘上的 dashboard 文件**，只在
``StaticFileService.resolve_static_file()`` 返回文件路径之前做一次改写：命中目标
locale chunk 时，返回一个写在本插件数据目录里的「改写副本」，内核照常用
``FileResponse`` / gzip 逻辑把它发出去。

**怎么知道哪个文件是哪个语言的**：入口 chunk 里保存着 ``localeLoaders`` 的映射，
形如 ``"zh-CN":()=>Ie(()=>import("./index-uFqO4ev5.js"))``。本模块从 ``index.html``
找到入口 chunk，解析出 ``{语言包文件名: locale}``，再按需改写。

**怎么插入词条**：locale chunk 里的词条是内联的 JS 对象字面量，例如
``...runnerDialog:{...},runners:{local:{title:"内置 AI",...}}``。这里的做法是按
:data:`ANCHORS` 里登记的锚点定位到目标对象（并要求邻域出现特征串，避免误伤），
在对象开头插入形如 ``"echo":{title:"Echo",summary:"…",description:"…"},`` 的键值对。

改写完全幂等、失败即放弃：任何一个环节不符合预期（找不到锚点、编码异常、写盘失败）
都会返回 None，让内核继续发送原始文件 —— 面板最多是多显示几个 ``[MISSING: …]``，
不会比没有本插件更糟。
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import registry

#: 被包裹的目标：AstrBot 的静态文件服务。
_STATIC_FILE_SERVICE_MODULE = "astrbot.dashboard.services.static_file_service"

#: 只处理这些后缀（locale 语言包是 ``.js``/``.mjs``）。
_TARGET_SUFFIXES = (".js", ".mjs")

#: 入口文档里引用的入口 chunk。
_ENTRY_SCRIPT_PATTERN = re.compile(r"""<script[^>]+src=["']([^"']+\.m?js)["']""")

#: 入口 chunk 里 ``localeLoaders`` 的映射：``"zh-CN":()=>…import("./index-xxxx.js")``。
_LOCALE_IMPORT_PATTERN = re.compile(
    r"""["'](?P<locale>[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)["']\s*:\s*"""
    r"""(?:\(\s*\)\s*=>|function\s*\(\s*\)\s*)[^,;{}]{0,120}?import\(\s*["']\./(?P<file>[^"']+\.m?js)["']"""
)

#: 兜底扫描时最多读取的文件数（避免在大目录里做无谓 IO）。
_MAX_SCAN_FILES = 400

#: 改写缓存目录名（位于插件数据目录下）。
ASSET_CACHE_DIR_NAME = "asset_cache"

_WRAPPED_ATTR = "_libi18nject_wrapped"
_ORIGINAL_ATTR = "_libi18nject_original"


@dataclass(frozen=True)
class Anchor:
    """一个「dotted key 前缀 → 打包产物插入点」的映射。

    Attributes:
        prefix: 词条前缀，例如 ``features.config.aiSettings.runners.``。
        marker: 插入点文本，例如 ``runners:{``。
        context: 插入点附近必须出现的特征串，用于避免误伤同名结构。
        window: 在 marker 之前查找 ``context`` 的字符窗口。
        probe: 判断「该键是否已存在」时向后查看的字符窗口。
    """

    prefix: str
    marker: str
    context: str
    window: int = 600
    probe: int = 4000


#: 已支持的注入锚点。要支持新的文案位置，只要在这里补一条即可。
ANCHORS: tuple[Anchor, ...] = (
    Anchor(
        prefix="features.config.aiSettings.runners.",
        marker="runners:{",
        context="runnerDialog:",
    ),
)


def _plugin_data_dir() -> Path | None:
    """返回本插件的数据目录；AstrBot 路径工具不可用时返回 None。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / registry.PLUGIN_NAME
    except Exception:  # noqa: BLE001 - 路径工具不可用时静默降级
        return None


def _group_entries(entries: Mapping[str, str]) -> dict[str, dict[str, dict[str, str]]]:
    """把 dotted key 按锚点分组。

    Args:
        entries: ``{dotted_key: text}``。

    Returns:
        ``{anchor_index: {root: {attr: text}}}``；无法归入任何锚点的键会被忽略
        （本插件只负责已知的注入点，写错前缀不会造成误注入）。
    """
    grouped: dict[int, dict[str, dict[str, str]]] = {}
    for key, text in entries.items():
        for index, anchor in enumerate(ANCHORS):
            if not key.startswith(anchor.prefix):
                continue
            rest = key[len(anchor.prefix) :]
            root, _, attr = rest.partition(".")
            if not root or not attr:
                logger.debug(
                    "libi18nject: 词条 %s 不符合 %s<root>.<attr> 形式，已跳过",
                    key,
                    anchor.prefix,
                )
                break
            grouped.setdefault(index, {}).setdefault(root, {})[attr] = text
            break
    return grouped


def _render_js_entries(roots: Mapping[str, Mapping[str, str]]) -> str:
    """把 ``{root: {attr: text}}`` 渲染成 JS 对象字面量的成员片段。

    Args:
        roots: 待插入的键值对。

    Returns:
        形如 ``"echo":{title:"Echo","description":"…"},`` 的文本（尾部带逗号，
        便于直接接在 ``{`` 之后）。
    """
    parts: list[str] = []
    for root, attrs in roots.items():
        body = ",".join(
            f"{json.dumps(attr, ensure_ascii=False)}:"
            f"{json.dumps(str(text), ensure_ascii=False)}"
            for attr, text in attrs.items()
        )
        parts.append(f"{json.dumps(root, ensure_ascii=False)}:{{{body}}}")
    return ",".join(parts) + "," if parts else ""


def inject_text(text: str, locale: str) -> tuple[str, int] | None:
    """把登记的词条注入到一段 locale chunk 文本里。

    Args:
        text: locale chunk 的源码。
        locale: 该 chunk 对应的语言，例如 ``zh-CN``。

    Returns:
        ``(改写后的文本, 插入的条数)``；无需改写（没有匹配的词条、找不到锚点、
        目标键已存在）时返回 None。
    """
    merged = registry.merged_entries()
    entries: dict[str, str] = {}
    entries.update(merged.get(registry.ANY_LOCALE) or {})
    entries.update(merged.get(locale) or {})
    if not entries:
        return None

    grouped = _group_entries(entries)
    if not grouped:
        return None

    for index, roots in grouped.items():
        anchor = ANCHORS[index]
        position = text.find(anchor.marker)
        if position < 0:
            continue
        head = text[max(0, position - anchor.window) : position]
        if anchor.context not in head:
            continue

        insert_at = position + len(anchor.marker)
        window_text = text[insert_at : insert_at + anchor.probe]
        pending = {
            root: attrs
            for root, attrs in roots.items()
            if f"{json.dumps(root, ensure_ascii=False)}:" not in window_text
        }
        if not pending:
            continue

        snippet = _render_js_entries(pending)
        if not snippet:
            continue
        return text[:insert_at] + snippet + text[insert_at:], len(pending)
    return None


class LocaleChunkIndex:
    """解析「语言包 chunk 文件名 → locale」的映射（按 dashboard 目录缓存）。"""

    def __init__(self) -> None:
        self._cache_key: tuple[Any, ...] | None = None
        self._mapping: dict[str, str] = {}

    def mapping(self, static_folder: str | Path | None) -> dict[str, str]:
        """返回 ``{语言包文件名: locale}``。

        Args:
            static_folder: dashboard dist 目录。

        Returns:
            映射表；无法解析时返回空表（调用方据此跳过改写）。
        """
        if not static_folder:
            return {}
        root = Path(static_folder)
        entry = self._entry_chunk(root)
        cache_key = (
            str(root),
            self._stamp(root / "index.html"),
            self._stamp(entry) if entry is not None else None,
        )
        if cache_key == self._cache_key:
            return self._mapping

        mapping: dict[str, str] = {}
        if entry is not None:
            mapping = self._parse_locale_imports(entry)
        if not mapping:
            mapping = self._scan_for_locale_imports(root)

        if mapping != self._mapping:
            logger.info(
                "libi18nject: 已识别 %d 个语言包 chunk：%s",
                len(mapping),
                ", ".join(f"{locale}={name}" for name, locale in sorted(mapping.items())),
            )
        self._cache_key = cache_key
        self._mapping = mapping
        return mapping

    @staticmethod
    def _stamp(path: Path | None) -> tuple[int, int] | None:
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _entry_chunk(self, root: Path) -> Path | None:
        """从 ``index.html`` 找出入口 chunk。"""
        try:
            index_html = (root / "index.html").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        match = _ENTRY_SCRIPT_PATTERN.search(index_html)
        if match is None:
            return None
        name = Path(match.group(1).split("?", 1)[0]).name
        for candidate in (root / "assets" / name, root / name):
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _parse_locale_imports(entry: Path) -> dict[str, str]:
        """从入口 chunk 解析 ``localeLoaders`` 映射。"""
        try:
            text = entry.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return {}
        mapping: dict[str, str] = {}
        for match in _LOCALE_IMPORT_PATTERN.finditer(text):
            mapping[Path(match.group("file")).name] = match.group("locale")
        return mapping

    def _scan_for_locale_imports(self, root: Path) -> dict[str, str]:
        """兜底：在没有 ``index.html`` 时扫描 assets 目录里的映射。"""
        assets = root / "assets"
        if not assets.is_dir():
            return {}
        try:
            candidates = sorted(assets.glob("*.js"))[:_MAX_SCAN_FILES]
        except OSError:
            return {}
        for candidate in candidates:
            mapping = self._parse_locale_imports(candidate)
            if mapping:
                return mapping
        return {}


class AssetInjector:
    """包装内核的静态文件服务，把注入过词条的 chunk 换成本地改写副本。"""

    def __init__(self) -> None:
        self._index = LocaleChunkIndex()
        self._original: Any = None
        self._service_module: Any = None
        self.applied = False
        self.last_error: str | None = None

    def apply(self) -> bool:
        """挂上补丁（幂等）。

        Returns:
            True 表示补丁已生效。
        """
        try:
            module = importlib.import_module(_STATIC_FILE_SERVICE_MODULE)
        except Exception as exc:  # noqa: BLE001 - 版本不兼容时不影响插件加载
            self.last_error = f"无法导入静态文件服务: {exc}"
            logger.warning("libi18nject: %s", self.last_error)
            return False

        holder = getattr(module, "StaticFileService", None)
        original = getattr(holder, "resolve_static_file", None) if holder else None
        if not callable(original):
            self.last_error = "未找到 StaticFileService.resolve_static_file"
            logger.warning("libi18nject: %s（assets 注入不可用）", self.last_error)
            return False
        if getattr(original, _WRAPPED_ATTR, False):
            # 上一次实例残留的补丁：取出更早的原始实现，避免层层包装。
            original = getattr(original, _ORIGINAL_ATTR, original)

        def resolve_static_file(service_self, static_folder, requested_path):
            real = original(service_self, static_folder, requested_path)
            if real is None:
                return None
            return self.rewrite_path(real, static_folder) or real

        setattr(resolve_static_file, _WRAPPED_ATTR, True)
        setattr(resolve_static_file, _ORIGINAL_ATTR, original)
        holder.resolve_static_file = resolve_static_file
        self._service_module = module
        self._original = original
        self.applied = True
        self.last_error = None
        logger.info("libi18nject: 已接管静态资源请求，将在响应层注入 WebUI 词条。")
        return True

    def revert(self) -> None:
        """撤销补丁。"""
        holder = getattr(self._service_module, "StaticFileService", None)
        if holder is not None and self._original is not None:
            current = getattr(holder, "resolve_static_file", None)
            if getattr(current, _WRAPPED_ATTR, False):
                holder.resolve_static_file = self._original
        self._original = None
        self._service_module = None
        self.applied = False

    def rewrite_path(
        self,
        file_path: Path,
        static_folder: str | Path | None,
    ) -> Path | None:
        """按需生成改写副本。

        Args:
            file_path: 内核原本要发送的文件。
            static_folder: dashboard dist 目录。

        Returns:
            改写副本路径；不需要或无法改写时返回 None（调用方使用原文件）。
        """
        fingerprint = registry.entries_fingerprint()
        if not fingerprint:
            return None
        if file_path.suffix.lower() not in _TARGET_SUFFIXES:
            return None

        locale = self._index.mapping(static_folder).get(file_path.name)
        if not locale:
            return None

        cache_dir = _plugin_data_dir()
        if cache_dir is None:
            return None

        try:
            stamp = file_path.stat()
        except OSError:
            return None

        name = f"{file_path.stem}.{locale}.{fingerprint}.{stamp.st_mtime_ns:x}{file_path.suffix}"
        target = cache_dir / ASSET_CACHE_DIR_NAME / name
        if target.is_file():
            return target

        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("libi18nject: 读取 %s 失败: %s", file_path, exc)
            return None

        result = inject_text(text, locale)
        if result is None:
            return None
        rewritten, count = result

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_text(rewritten, encoding="utf-8")
            os.replace(temporary, target)
        except OSError as exc:
            logger.warning("libi18nject: 写入改写副本失败: %s", exc)
            return None

        logger.info(
            "libi18nject: 已为 %s（%s）注入 %d 项 WebUI 文案，改用 %s",
            file_path.name,
            locale,
            count,
            target,
        )
        return target
