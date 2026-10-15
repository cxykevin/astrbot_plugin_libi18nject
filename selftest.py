"""astrbot_plugin_libi18nject 的离线自测 / 回归检查。

覆盖：注册表合并与指纹、词条注入（含幂等与「目标键已存在」的跳过）、语言包映射解析、
请求层改写副本与还原、纯文本分组保护与还原、`/libi18n` 状态文本，以及（可用时）
对真实 dashboard 语言包的注入 + node 语法校验。

用法（在 AstrBot 仓库中，使用该实例的 uv 环境）::

    uv run python data/plugins/astrbot_plugin_libi18nject/selftest.py

脚本会把 ``ASTRBOT_ROOT`` 指向临时目录（除非调用方已显式指定），因此改写缓存写在临时
目录里，不会污染真实实例。全部检查通过时退出码为 0。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")
if "ASTRBOT_ROOT" not in os.environ:
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="libi18nject-selftest-")

try:  # 以 astrbot 包的位置定位仓库根，避免依赖插件目录的层级
    import astrbot

    _REPO_ROOT = Path(astrbot.__file__).resolve().parents[1]
except ImportError:  # 回退：向上找含 astrbot 包的目录（不依赖插件目录层级）
    _REPO_ROOT = next(
        (
            parent
            for parent in Path(__file__).resolve().parents
            if (parent / "astrbot" / "__init__.py").is_file()
        ),
        Path(__file__).resolve().parents[2],
    )
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from astrbot.core.config.i18n_utils import ConfigMetadataI18n
from astrbot.dashboard.services.static_file_service import StaticFileService

from data.plugins.astrbot_plugin_libi18nject import registry
from data.plugins.astrbot_plugin_libi18nject.api import (
    ANY_LOCALE,
    PLUGIN_NAME,
    get_status,
    register_i18n_entries,
    register_plain_text_sections,
    supported_key_prefixes,
    unregister,
)
from data.plugins.astrbot_plugin_libi18nject.injector import (
    ASSET_CACHE_DIR_NAME,
    ANCHORS,
    AssetInjector,
    LocaleChunkIndex,
    inject_text,
)
from data.plugins.astrbot_plugin_libi18nject.main import LibI18njectPlugin
from data.plugins.astrbot_plugin_libi18nject.metadata_i18n import MetadataI18nPatcher

FAILURES: list[str] = []

RUNNER_TYPE = "probe"
KEY_PREFIX = f"features.config.aiSettings.runners.{RUNNER_TYPE}."

#: 模仿真实 locale chunk 的片段（键无引号、值双引号，与 vite 打包 JSON 模块一致）。
CHUNK_TEMPLATE = (
    "var cfg={tabs:{model:\"模型\"},"
    "runnerDialog:{title:\"更换执行方式\",confirm:\"使用此方式\"},"
    "runners:{local:{title:\"内置 AI\",summary:\"配置 AstrBot 的 AI 能力。\","
    "description:\"使用 AstrBot 内置 AI。\"},dify:{title:\"Dify\"}}};"
)


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条检查结果。"""
    print(
        f"[{'PASS' if condition else 'FAIL'}] {name}"
        + (f"  <- {detail}" if detail and not condition else "")
    )
    if not condition:
        FAILURES.append(name)


def _zh_entries(**overrides: str) -> dict[str, dict[str, str]]:
    """构造一份中文词条（可选覆盖）。"""
    table = {
        f"{KEY_PREFIX}title": "探针",
        f"{KEY_PREFIX}summary": "当前由探针执行器处理 AI 对话。",
        f"{KEY_PREFIX}description": "连接探针服务并配置访问参数。",
    }
    table.update({f"{KEY_PREFIX}{key}": value for key, value in overrides.items()})
    return {"zh-CN": table}


def _node_check(path: Path) -> bool | None:
    """用 node 校验 JS 语法。

    Args:
        path: 待校验文件。

    Returns:
        True 语法正确、False 语法错误、None 表示环境里没有 node（跳过）。
    """
    node = shutil.which("node")
    if node is None:
        return None
    completed = subprocess.run(
        [node, "--check", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _make_dist(root: Path) -> Path:
    """构造一个最小的 dashboard dist 目录。"""
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        '<!doctype html><html><body><div id="app"></div>'
        '<script type="module" crossorigin src="/assets/entry-abc.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    (assets / "entry-abc.js").write_text(
        'const L={"zh-CN":()=>Ie(()=>import("./zh-abc.js")),'
        '"en-US":()=>Ie(()=>import("./en-abc.js"))};export{L};',
        encoding="utf-8",
    )
    (assets / "zh-abc.js").write_text(CHUNK_TEMPLATE, encoding="utf-8")
    (assets / "en-abc.js").write_text(
        CHUNK_TEMPLATE.replace("更换执行方式", "Change runner"), encoding="utf-8"
    )
    return root


def _plugin_data_dir() -> Path:
    """返回本次自测使用的插件数据目录。"""
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME


def _reset_registry() -> None:
    """清空注册表（每条用例前调用）。"""
    for owner in list(registry._shared_state()["entries"]):
        registry.drop_entries(owner)
    for owner in list(registry._shared_state()["sections"]):
        registry.drop_plain_sections(owner)


# ------------------------------------------------------------- 0. 基础约定
check("插件名与文档一致", PLUGIN_NAME == "astrbot_plugin_libi18nject")
check(
    "支持的前缀取自锚点表",
    supported_key_prefixes() == tuple(a.prefix for a in ANCHORS)
    and "features.config.aiSettings.runners." in supported_key_prefixes(),
    str(supported_key_prefixes()),
)
check("未登记时指纹为空", registry.entries_fingerprint() == "")

# ------------------------------------------------------------- 1. 注册表与 API
count = register_i18n_entries(_zh_entries(), owner="selftest")
check("register_i18n_entries 返回接受条数", count == 3, str(count))
entries = registry.merged_entries()
check("词条按语言合并", sorted(entries) == ["zh-CN"], str(sorted(entries)))
check("词条内容正确", entries["zh-CN"][f"{KEY_PREFIX}title"] == "探针")

fingerprint = registry.entries_fingerprint()
check("登记后指纹非空", bool(fingerprint), fingerprint)
register_i18n_entries(_zh_entries(title="探针 2"), owner="selftest")
check("重复登记是覆盖式的", registry.merged_entries()["zh-CN"][f"{KEY_PREFIX}title"] == "探针 2")
check("内容变化后指纹变化", registry.entries_fingerprint() != fingerprint)

ignored = register_i18n_entries(
    {"zh-CN": {"features.config.unknown.path": "x", f"{KEY_PREFIX}title": "探针 3"}},
    owner="selftest",
)
check("不支持的前缀被忽略", ignored == 1, str(ignored))
check(
    "忽略后合法词条仍被登记",
    registry.merged_entries()["zh-CN"][f"{KEY_PREFIX}title"] == "探针 3",
)

try:
    register_i18n_entries({"zh-CN": "oops"}, owner="selftest")  # type: ignore[arg-type]
    check("非法 entries 结构被拒绝", False)
except TypeError:
    check("非法 entries 结构被拒绝", True)

register_i18n_entries({ANY_LOCALE: {"features.config.aiSettings.runners.probe.summary": "通用"}}, owner="wild")
check("通配 locale 也能登记", ANY_LOCALE in registry.merged_entries())
check("多个 owner 共存", get_status()["owners"] == ["selftest", "wild"], str(get_status()["owners"]))
check("unregister 清理两个 owner", unregister(owner="wild"))
check("清理后只剩一个 owner", registry.get_status()["owners"] == ["selftest"])

listener_calls: list[int] = []


def _on_registry_change() -> None:
    listener_calls.append(1)


registry.add_listener(_on_registry_change)
register_i18n_entries(_zh_entries(), owner="selftest")
check("注册变化会通知监听者", bool(listener_calls), str(len(listener_calls)))
registry.remove_listener(_on_registry_change)
register_i18n_entries(_zh_entries(title="探针 4"), owner="selftest")
check("移除监听后不再通知", len(listener_calls) == 1, str(len(listener_calls)))

# ------------------------------------------------------------- 2. 纯文本分组
register_plain_text_sections(
    lambda: {"probe_runner": {"description": "探针", "items": {"agent_runner.config.a": {"description": "A"}}}},
    owner="selftest",
)
sections = registry.merged_plain_sections()
check("纯文本分组被收集", list(sections) == ["probe_runner"], str(list(sections)))
check("get_status 汇总分组", get_status()["plain_sections"] == ["probe_runner"])


def _boom():
    raise RuntimeError("boom")


registry.set_plain_sections("broken", _boom)
check("坏 getter 不影响其它分组", list(registry.merged_plain_sections()) == ["probe_runner"])
registry.drop_plain_sections("broken")

# ------------------------------------------------------------- 3. 词条注入
result = inject_text(CHUNK_TEMPLATE, "zh-CN")
check("命中锚点时发生注入", result is not None)
rewritten, inserted = result if result else ("", 0)
check("插入条数正确", inserted == 1, str(inserted))
check("注入内容进入 runners 对象", 'runners:{"probe":{' in rewritten, rewritten[:160])
check("注入的标题正确", '"title":"探针' in rewritten, rewritten[:200])
check("未命中语言时不注入", inject_text(CHUNK_TEMPLATE, "fr-FR") is None)
check("重复注入被跳过", inject_text(rewritten, "zh-CN") is None)
check(
    "缺少邻域特征串时不注入",
    inject_text('var x={runners:{local:{title:"内置 AI"}}};', "zh-CN") is None,
)
target = Path(tempfile.mkdtemp(prefix="libi18nject-js-")) / "chunk.js"
target.write_text(rewritten, encoding="utf-8")
node_result = _node_check(target)
check("改写后的 JS 语法正确", node_result is not False, "node --check 失败" if node_result is False else "skipped: no node")

# ------------------------------------------------------------- 4. 语言包映射
dist = _make_dist(Path(tempfile.mkdtemp(prefix="libi18nject-dist-")))
index = LocaleChunkIndex()
mapping = index.mapping(dist)
check(
    "从入口 chunk 解析出语言包映射",
    mapping == {"zh-abc.js": "zh-CN", "en-abc.js": "en-US"},
    str(mapping),
)
check("重复调用命中缓存", index.mapping(dist) == mapping)

# ------------------------------------------------------------- 5. 请求层改写与还原
injector = AssetInjector()
check("AssetInjector.apply()", injector.apply(), injector.last_error or "")
service = StaticFileService()
original_path = dist / "assets" / "zh-abc.js"
resolved = service.resolve_static_file(str(dist), "assets/zh-abc.js")
check("命中语言包时改用改写副本", resolved is not None and resolved != original_path, str(resolved))
check(
    "改写副本位于插件数据目录",
    bool(resolved) and _plugin_data_dir() in Path(resolved).parents,
    str(resolved),
)
rewritten_copy = Path(resolved).read_text(encoding="utf-8") if resolved else ""
check("改写副本包含注入词条", '"probe"' in rewritten_copy, rewritten_copy[:160])
check("原始文件保持不变", original_path.read_text(encoding="utf-8") == CHUNK_TEMPLATE)
check(
    "非语言包文件不受影响",
    service.resolve_static_file(str(dist), "assets/entry-abc.js") == dist / "assets" / "entry-abc.js",
)
unregistered_locale = service.resolve_static_file(str(dist), "assets/en-abc.js")
check("未登记语言的语言包不改写", unregistered_locale == dist / "assets" / "en-abc.js")
check(
    "缓存目录已创建",
    (_plugin_data_dir() / ASSET_CACHE_DIR_NAME).is_dir(),
)
injector.revert()
check(
    "revert 后恢复原始解析",
    service.resolve_static_file(str(dist), "assets/zh-abc.js") == original_path,
)
check("revert 后补丁状态复位", injector.applied is False)

# ------------------------------------------------------------- 6. 纯文本分组保护
patcher = MetadataI18nPatcher()
check("MetadataI18nPatcher.apply()", patcher.apply(), patcher.last_error or "")
metadata = {
    "ai_group": {
        "metadata": {
            "probe_runner": {
                "description": "探针",
                "items": {"agent_runner.config.a": {"description": "A"}},
            },
            "coze_runner": {"description": "Coze 配置", "items": {}},
        }
    }
}
converted = ConfigMetadataI18n.convert_to_i18n_keys(metadata)["ai_group"]["metadata"]
check("登记的分组保持纯文本", converted["probe_runner"]["description"] == "探针", str(converted))
check(
    "登记分组的字段文案保持纯文本",
    converted["probe_runner"]["items"]["agent_runner.config.a"]["description"] == "A",
    str(converted["probe_runner"]),
)
check(
    "未登记的内核分组仍转成 i18n key",
    converted["coze_runner"]["description"] == "ai_group.coze_runner.description",
    str(converted["coze_runner"]),
)
patcher.revert()
restored = ConfigMetadataI18n.convert_to_i18n_keys(metadata)["ai_group"]["metadata"]
check(
    "revert 后恢复内核行为",
    restored["probe_runner"]["description"] == "ai_group.probe_runner.description",
    str(restored["probe_runner"]),
)

# ------------------------------------------------------------- 7. 插件生命周期
plugin = LibI18njectPlugin(context=None, config={})
asyncio.run(plugin.initialize())
status = get_status()
check("initialize 后 assets 注入生效", status["asset_patched"] is True, str(status))
check("initialize 后 metadata 补丁生效", status["metadata_patched"] is True, str(status))
check("初始化无错误", not status["patch_error"], str(status["patch_error"]))
text = plugin._render_status()
check("状态文本含两部分状态", "assets 注入: 已启用" in text and "metadata 纯文本保护: 已启用" in text, text)
check("状态文本含注册方", "selftest" in text, text)
check("状态文本含词条统计", "词条:" in text, text)
check("状态文本含纯文本分组", "probe_runner" in text, text)

off = LibI18njectPlugin(context=None, config={"enable_asset_injection": False})
asyncio.run(off.initialize())
check("配置可关闭 assets 注入", get_status()["asset_patched"] is False)
check("关闭状态下仍启用 metadata 补丁", get_status()["metadata_patched"] is True)
asyncio.run(off.terminate())
asyncio.run(plugin.terminate())
check("terminate 后两部分都复位", get_status()["asset_patched"] is False and get_status()["metadata_patched"] is False)

# ------------------------------------------------------------- 8. 真实语言包演练（可选）
real_dist = _REPO_ROOT / "data" / "dist"
real_index = LocaleChunkIndex().mapping(real_dist)
if real_index:
    chunk_name, locale_name = next(iter(sorted(real_index.items())))
    real_file = real_dist / "assets" / chunk_name
    check("真实 dist 中识别到语言包", real_file.is_file(), str(real_file))
    register_i18n_entries({locale_name: _zh_entries()["zh-CN"]}, owner="selftest")
    real_text = real_file.read_text(encoding="utf-8")
    injected = inject_text(real_text, locale_name)
    if injected is None:
        check("真实语言包可注入", False, "锚点未命中（dashboard 打包结构可能已变化）")
    else:
        body, count = injected
        check("真实语言包可注入", count == 1, str(count))
        sample_path = Path(tempfile.mkdtemp(prefix="libi18nject-real-")) / "chunk.mjs"
        sample_path.write_text(body, encoding="utf-8")
        real_node = _node_check(sample_path)
        check(
            "真实语言包改写后语法正确",
            real_node is not False,
            "node --check 失败" if real_node is False else "skipped: no node",
        )
        check("改写后内容变长（插入了词条）", len(body) > len(real_text))
else:
    check("真实 dist 中识别到语言包", False, f"未找到语言包映射（{real_dist} 可能不存在）")

_reset_registry()
check("清理后注册表为空", registry.entries_fingerprint() == "")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
