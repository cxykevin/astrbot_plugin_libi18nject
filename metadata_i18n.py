"""让登记过的配置分组保持纯文本，而不是变成找不到词条的 i18n key。

内核在把 metadata 交给 WebUI 之前会调用
``ConfigMetadataI18n.convert_to_i18n_keys()``，它把每个分组与字段的
``description`` / ``hint`` / ``labels`` / ``name`` **无条件**替换成
``ai_group.<section>.<field>.description`` 形式的 key —— 因为内核自带分组的词条都
写在 dashboard 的静态语言文件里。注入方（例如 librunner 注册的 ``echo_runner``）
没有这些词条，替换后面板只会显示 ``[MISSING: …]``。

这里包一层：仍然交给内核原始实现转换，再把注册方声明过的 section 换回纯文本。
前端 ``translateIfKey()`` 在翻译表里查不到 key 时会原样显示文本，因此纯文本是安全
的；内核自带分组的转换行为完全不变。

（另一半 i18n 能力 —— 往 WebUI 打包产物里补词条 —— 见 :mod:`injector`。）
"""

from __future__ import annotations

import importlib
from typing import Any

from astrbot.api import logger

from . import registry

#: 被包裹的目标：内核的配置元数据 i18n 转换器。
_METADATA_I18N_MODULE = "astrbot.core.config.i18n_utils"

_WRAPPED_ATTR = "_libi18nject_wrapped"
_ORIGINAL_ATTR = "_libi18nject_original"


class MetadataI18nPatcher:
    """包装 ``ConfigMetadataI18n.convert_to_i18n_keys``。"""

    def __init__(self) -> None:
        self._original: Any = None
        self._module: Any = None
        self.applied = False
        self.last_error: str | None = None

    def apply(self) -> bool:
        """挂上补丁（幂等）。

        Returns:
            True 表示补丁已生效。
        """
        try:
            module = importlib.import_module(_METADATA_I18N_MODULE)
        except Exception as exc:  # noqa: BLE001 - 版本不兼容时不影响插件加载
            self.last_error = f"无法导入配置元数据 i18n 模块: {exc}"
            logger.warning("libi18nject: %s", self.last_error)
            return False

        holder = getattr(module, "ConfigMetadataI18n", None)
        original = (
            getattr(holder, "convert_to_i18n_keys", None) if holder is not None else None
        )
        if not callable(original):
            self.last_error = "未找到 ConfigMetadataI18n.convert_to_i18n_keys"
            logger.warning("libi18nject: %s（纯文本分组保护不可用）", self.last_error)
            return False
        if getattr(original, _WRAPPED_ATTR, False):
            # 上一次实例残留的补丁：取出更早的原始实现，避免层层包装。
            original = getattr(original, _ORIGINAL_ATTR, original)

        def convert_to_i18n_keys(metadata: Any) -> dict[str, Any]:
            result = original(metadata)
            try:
                sections = (
                    result.get("ai_group", {}).get("metadata")
                    if isinstance(result, dict)
                    else None
                )
                if isinstance(sections, dict):
                    for name, plain in registry.merged_plain_sections().items():
                        if name in sections:
                            sections[name] = plain
            except Exception:  # noqa: BLE001 - 面板文案失败不影响功能
                logger.debug("libi18nject: 还原纯文本分组失败", exc_info=True)
            return result

        setattr(convert_to_i18n_keys, _WRAPPED_ATTR, True)
        setattr(convert_to_i18n_keys, _ORIGINAL_ATTR, original)
        holder.convert_to_i18n_keys = staticmethod(convert_to_i18n_keys)
        self._module = module
        self._original = original
        self.applied = True
        self.last_error = None
        logger.info(
            "libi18nject: 已接管配置元数据文案转换，登记过的分组将保持纯文本。"
        )
        return True

    def revert(self) -> None:
        """撤销补丁。"""
        holder = getattr(self._module, "ConfigMetadataI18n", None)
        if holder is not None and self._original is not None:
            current = getattr(holder, "convert_to_i18n_keys", None)
            if getattr(current, _WRAPPED_ATTR, False):
                holder.convert_to_i18n_keys = staticmethod(self._original)
        self._original = None
        self._module = None
        self.applied = False
