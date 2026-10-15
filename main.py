"""astrbot_plugin_libi18nject —— 在请求层给 AstrBot WebUI 补 i18n 文案的插件。

dashboard 的界面文案（例如「更换执行方式」对话框里每个执行器的标题与说明）全部
打包在构建产物里，插件侧原本没有任何注入通道，缺少词条时页面只会显示
``[MISSING: features.config.aiSettings.runners.echo.title]`` 这类占位。

本插件提供：

* ``api`` 模块：公共注入接口，其它插件登记自己的 WebUI 词条，以及需要保持纯文本的
  metadata 分组；
* ``injector`` 模块：包装内核的静态文件服务，在语言包送到浏览器**之前**把词条写进
  改写副本（磁盘上的 dashboard 文件保持原样）；
* ``metadata_i18n`` 模块：让登记过的 metadata 分组跳过内核的 i18n key 转换；
* ``/libi18n`` 指令：查看注入状态。

插件元数据（名称/作者/版本/依赖的 AstrBot 版本）以 ``metadata.yaml`` 为准，因此这里
不使用已废弃的 ``@register`` 装饰器。
"""

from __future__ import annotations

from collections.abc import Mapping

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import registry
from .injector import AssetInjector
from .metadata_i18n import MetadataI18nPatcher

__version__ = "v1.0.0"


class LibI18njectPlugin(Star):
    """Libi18nject · WebUI 文案注入。

    启动时会做两件事（都可以在插件配置里关掉）：

    1. 接管 ``StaticFileService.resolve_static_file``：命中语言包 chunk 时返回写有
       注入方词条的改写副本；
    2. 接管 ``ConfigMetadataI18n.convert_to_i18n_keys``：把登记过的 metadata 分组
       换回纯文本，避免出现 ``[MISSING: ai_group.echo_runner.description]``。

    两部分补丁都会在 ``terminate()`` 时完整还原，且都能容忍「上一次实例残留的补丁」，
    所以热重载不会叠加包装。

    指令：
    - ``/libi18n``：查看两部分补丁的状态与当前登记的注册方。
    """

    def __init__(self, context: Context, config: Mapping | None = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 插件上下文。
            config: 插件配置（``_conf_schema.json`` 的实例化结果）。
        """
        super().__init__(context)
        self.plugin_config = config if isinstance(config, Mapping) else {}
        self._injector: AssetInjector | None = None
        self._metadata: MetadataI18nPatcher | None = None

    async def initialize(self) -> None:
        """插件实例化后由 AstrBot 自动调用：挂上两部分补丁。"""
        errors: list[str] = []

        if self._flag("enable_asset_injection", True):
            injector = AssetInjector()
            if injector.apply():
                self._injector = injector
            else:
                errors.append(injector.last_error or "assets 注入未生效")

        if self._flag("enable_metadata_plain_text", True):
            patcher = MetadataI18nPatcher()
            if patcher.apply():
                self._metadata = patcher
            else:
                errors.append(patcher.last_error or "metadata 补丁未生效")

        registry.set_patch_state(
            asset_patched=self._injector is not None,
            metadata_patched=self._metadata is not None,
            patch_error="；".join(errors) or None,
        )
        if errors:
            logger.warning("libi18nject: 部分能力未生效: %s", "；".join(errors))

    async def terminate(self) -> None:
        """插件被停用、卸载或重载时由 AstrBot 调用：撤销补丁。"""
        if self._injector is not None:
            self._injector.revert()
            self._injector = None
        if self._metadata is not None:
            self._metadata.revert()
            self._metadata = None
        registry.set_patch_state(asset_patched=False, metadata_patched=False)

    def _flag(self, key: str, default: bool) -> bool:
        """读取布尔配置项。

        Args:
            key: 配置项名。
            default: 配置缺失或不是布尔值时的返回值。

        Returns:
            布尔配置值。
        """
        value = self.plugin_config.get(key, default)
        return value if isinstance(value, bool) else default

    @filter.command("libi18n")
    async def libi18n(self, event: AstrMessageEvent):
        """查看 WebUI 文案注入状态与注册方"""
        yield event.plain_result(self._render_status())

    def _render_status(self) -> str:
        """渲染 ``/libi18n`` 的文本输出。"""
        status = registry.get_status()
        lines = [
            f"libi18nject {__version__}",
            f"assets 注入: {'已启用' if status['asset_patched'] else '未启用'}",
            f"metadata 纯文本保护: {'已启用' if status['metadata_patched'] else '未启用'}",
        ]
        if status["patch_error"]:
            lines.append(f"未生效原因: {status['patch_error']}")

        owners = status["owners"]
        if owners:
            lines.append(f"注册方（{len(owners)}）: {', '.join(owners)}")
        else:
            lines.append("注册方: 无（暂无插件调用 register_i18n_entries）")

        locales = ", ".join(status["locales"]) or "无"
        lines.append(f"词条: {status['entries']} 条，语言: {locales}")
        if status["plain_sections"]:
            lines.append(f"纯文本分组: {', '.join(status['plain_sections'])}")
        return "\n".join(lines)
