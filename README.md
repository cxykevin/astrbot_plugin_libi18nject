# astrbot_plugin_libi18nject

> 在**请求层**给 AstrBot WebUI 补 i18n 文案：让插件能为自己注入的界面元素提供文案，
> 从根上消除面板上的 `[MISSING: ...]` 占位。
> **不修改任何 dashboard 文件，也不重新构建 `data/dist`。**

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D%204.29.0-blue)
![License](https://img.shields.io/badge/license-AGPL--3.0-green)

- 🧩 `register_i18n_entries()`：登记 WebUI 词条（例如「更换执行方式」对话框里
  执行器的标题/说明）；
- 🛡️ `register_plain_text_sections()`：登记需要保持纯文本的配置分组，跳过内核的
  i18n key 转换；
- 🔁 两部分能力都可以在插件配置里单独关闭，`terminate()` 时完整还原；
- 🧯 任何一环不符合预期都**安静放弃**（继续发送原始文件），不会让 WebUI 打不开。

> [!IMPORTANT]
> 基于 **AstrBot v4.29.0** 的 `StaticFileService` 与 `ConfigMetadataI18n` 实现，
> 通过 monkey patch 接入（`metadata.yaml` 已声明 `astrbot_version: ">=4.29.0"`）。
> 升级 AstrBot 后请先用 `/libi18n` 确认两部分补丁仍然生效。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [安装](#安装)
- [快速开始（注入方插件）](#快速开始注入方插件)
- [API 参考](#api-参考)
- [工作原理](#工作原理)
- [配置](#配置)
- [已知限制与风险](#已知限制与风险)
- [故障排查](#故障排查)
- [开发者自测](#开发者自测)
- [与 astrbot_plugin_librunner 的关系](#与-astrbot_plugin_librunner-的关系)
- [License](#license)

---

## 它解决什么问题

dashboard 的界面文案（`features.config.aiSettings.runners.<type>.title` 等）是**打包进
前端产物**的，后端没有任何运行时注入通道 —— 插件注册的新执行器因此会在
「更换执行方式」对话框和页面标题里显示成：

```
[MISSING: features.config.aiSettings.runners.echo.title]
```

另一处类似问题是内核的 `ConfigMetadataI18n.convert_to_i18n_keys()`：它会把 metadata 中
每个分组/字段的 `description` / `hint` / `labels` 无条件换成 `ai_group.<section>...`
形式的 key，注入方没有词条就会显示：

```
[MISSING: ai_group.echo_runner.description]
```

本插件用一个统一的地方处理这两类问题。注入方插件只需要在启动时登记自己的文案，
不需要关心底层是怎么送进浏览器的，也不必修改内核文件。

---

## 安装

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/cxykevin/astrbot_plugin_libi18nject.git
```

也可以直接把插件目录拷进 `data/plugins/`，再重启 AstrBot（或在 WebUI 的插件页重载）。

本插件**独立工作、不依赖任何注入方**：没有注入方时它只是注册表为空，不产生任何
影响；与服务它的注入方（例如 `astrbot_plugin_librunner`）各自启停即可。

启动日志：

```
libi18nject: 已接管静态资源请求，将在响应层注入 WebUI 词条。
libi18nject: 已接管配置元数据文案转换，登记过的分组将保持纯文本。
```

在聊天里发送 `/libi18n` 查看状态：

```
libi18nject v1.0.0
assets 注入: 已启用
metadata 纯文本保护: 已启用
注册方（1）: astrbot_plugin_librunner
词条: 3 条，语言: *（librunner 登记到通配 locale，对所有语言生效）
纯文本分组: echo_runner
```

---

## 快速开始（注入方插件）

```python
from data.plugins.astrbot_plugin_libi18nject.api import (
    register_i18n_entries,
    register_plain_text_sections,
    unregister,
)

RUNNER_TYPE = "myagent"


class MyPlugin(Star):
    async def initialize(self):
        # 1) WebUI 词条：键是「模块.路径」，值是文案
        register_i18n_entries(
            {
                "zh-CN": {
                    f"features.config.aiSettings.runners.{RUNNER_TYPE}.title": "MyAgent",
                    f"features.config.aiSettings.runners.{RUNNER_TYPE}.summary": "当前由 MyAgent 处理 AI 对话。",
                    f"features.config.aiSettings.runners.{RUNNER_TYPE}.description": "连接 MyAgent 服务并配置访问参数。",
                },
                "en-US": {
                    f"features.config.aiSettings.runners.{RUNNER_TYPE}.title": "MyAgent",
                    f"features.config.aiSettings.runners.{RUNNER_TYPE}.summary": "AI chats are handled by MyAgent.",
                    f"features.config.aiSettings.runners.{RUNNER_TYPE}.description": "Connect to the MyAgent service.",
                },
                "*": {"features.config.aiSettings.runners.myagent.title": "MyAgent"},
            },
            owner="astrbot_plugin_myagent",
        )

        # 2) 需要保持纯文本的 metadata 分组（惰性求值，可随时返回最新内容）
        register_plain_text_sections(
            lambda: {"myagent_runner": {"description": "MyAgent", "items": {...}}},
            owner="astrbot_plugin_myagent",
        )

    async def terminate(self):
        unregister(owner="astrbot_plugin_myagent")
```

三个要点：

1. **随时重发**：登记是覆盖式的，注册表变化后整体重发一次即可；
2. **惰性分组**：`register_plain_text_sections()` 收的是 **getter**，每次请求元数据时现取，
   所以分组内容可以随你的注册表实时变化；
3. **成对注销**：`terminate()` 里调一次 `unregister(owner=...)` 就能清掉该 owner 的全部登记。

---

## API 参考

```python
from data.plugins.astrbot_plugin_libi18nject.api import ...
```

| 名称 | 说明 |
| --- | --- |
| `register_i18n_entries(entries, *, owner=None) -> int` | 登记（覆盖）WebUI 词条，返回被接受的条数 |
| `unregister_i18n_entries(*, owner=None) -> bool` | 移除某个 owner 的词条 |
| `register_plain_text_sections(getter, *, owner=None)` | 登记需要保持纯文本的配置分组 |
| `unregister_plain_text_sections(*, owner=None) -> bool` | 移除纯文本分组登记 |
| `unregister(*, owner=None) -> bool` | 一次性移除该 owner 的全部登记 |
| `supported_key_prefixes() -> tuple[str, ...]` | 当前支持注入的词条前缀 |
| `get_status() -> dict` | 补丁状态与注册情况 |
| `ANY_LOCALE` / `PLUGIN_NAME` | 通配 locale（`"*"`）/ 插件名常量 |

`owner` 缺省时从调用栈推断（取调用方的插件目录名），建议显式传插件名。

### 词条键的写法

- 目前支持的注入点是 `features.config.aiSettings.runners.<type>.<attr>`（见
  `supported_key_prefixes()`），`<attr>` 通常是 `title` / `summary` / `description`；
- 前缀不在支持范围内的键会被**忽略并打印警告**（避免「看起来注册了其实没生效」）；
- locale 用 dashboard 的语言代码（`zh-CN` / `en-US` / `ja-JP` / `ru-RU`）；`"*"` 表示
  所有语言的兜底，语言专属词条优先；
- 登记是覆盖式的：同一个 owner 重复登记同一个键，以最后一次为准 —— 注入方可以在
  注册表变化后整体重发一次。

---

## 工作原理

本插件挂两个补丁，都记录原始对象、可完整还原，并且能识别「上一次实例残留的补丁」
（热重载不会层层包装）。

### 1. 请求层改写 assets（`injector.py`）

静态资源由内核的 `StaticFileService.resolve_static_file()` 解析成文件路径，再交给
`FileResponse` / gzip 逻辑发送。补丁包装的就是这个**纯函数**：

1. 先调用内核原实现拿到真实文件路径；
2. 判断它是不是某个语言的**语言包 chunk** —— 映射来自入口 chunk 里的
   `localeLoaders`（`"zh-CN":()=>…import("./index-xxxx.js")`，入口 chunk 由
   `index.html` 定位）；
3. 是的话，把登记的词条插入到该 chunk 的 `runners:{…}` 对象开头（插入点由
   `ANCHORS` 描述，并要求邻域出现 `runnerDialog:` 特征串，避免误伤同名结构）；
4. 改写结果写到**本插件的数据目录**（`asset_cache/`，文件名含源文件 mtime 与词条
   指纹），返回这个副本路径，内核随后照常用原有的 gzip / Range / 头处理发送它。

因此：磁盘上的 `data/dist` 一字未动，浏览器拿到的却是补齐文案的版本；停用插件后
缓存副本不再被引用，重启进程也不会残留影响。

### 2. 保护纯文本 metadata 分组（`metadata_i18n.py`）

包装 `ConfigMetadataI18n.convert_to_i18n_keys()`：仍然调用内核实现完成转换，再把注入方
登记过的 section 换回纯文本。前端 `translateIfKey()` 在翻译表里查不到 key 时会原样显示
文本，所以纯文本是安全的；内核自带分组的转换行为完全不变。

---

## 配置

| 配置项 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable_asset_injection` | bool | `true` | 是否在响应层注入 WebUI 文案 |
| `enable_metadata_plain_text` | bool | `true` | 是否保护纯文本配置分组 |

两项都可以独立关闭，用于排查问题（例如怀疑是 assets 改写导致页面异常）。

---

## 已知限制与风险

- **与内核版本耦合**：依赖 `StaticFileService.resolve_static_file`、
  `ConfigMetadataI18n.convert_to_i18n_keys` 与打包产物的结构。任何一环不匹配都会
  记录日志并放弃注入（页面退回 `[MISSING: ...]`），不会破坏 WebUI。
- **只能补「已有注入点」的键**：目前是 `features.config.aiSettings.runners.*`。要支持
  新的文案位置，需要在 `injector.ANCHORS` 里补一条锚点（改动很小）。
- **语言包结构变化时会自动放弃**：如果 dashboard 换了打包方式（例如把 JSON 换成
  `JSON.parse('…')` 字符串），锚点匹配不上，注入会静默跳过。
- **改写产物是缓存文件**：位于 `data/plugin_data/astrbot_plugin_libi18nject/asset_cache/`，
  源文件或词条变化后会生成新文件；可以随时手动清空该目录。
- **不是官方 i18n 机制的替代品**：插件自己的配置页/视图请用官方
  `.astrbot-plugin/i18n/<locale>.json`；本插件补的是内核命名空间（插件 i18n 覆盖不到的
  地方）。

---

## 故障排查

| 现象 | 排查 |
| --- | --- |
| `/libi18n` 显示「assets 注入: 未启用」 | 看「未生效原因」：通常是 AstrBot 版本不匹配（符号缺失），或配置里关掉了该能力 |
| 面板仍然是 `[MISSING: ...]` | 确认注入方已加载（`/libi18n` 的「注册方」里能看到它）、当前浏览器语言在已登记的语言列表里；然后**强制刷新**浏览器缓存 |
| 页面样式/脚本异常 | 先把 `enable_asset_injection` 关掉重启，确认是否由改写引起；同时清空 `asset_cache/` 目录 |
| `ai_group.<type>_runner.description` 显示成 key | `enable_metadata_plain_text` 被关掉了，或注入方没有登记该分组 |
| 日志出现「词条 … 不在支持的范围内」 | 词条前缀写错，用 `supported_key_prefixes()` 查可用前缀 |

---

## 开发者自测

```bash
cd /path/to/AstrBot
uv run python data/plugins/astrbot_plugin_libi18nject/selftest.py
```

自测覆盖 61 项检查：注册表登记/合并/指纹、词条注入（含幂等、已存在键的跳过与坏 getter
隔离）、语言包映射解析、请求层改写与还原（含真实 `data/dist` chunk 的注入演练与改写后
JS 语法校验）、纯文本分组保护与还原，以及 `/libi18n` 状态文本。脚本会把 `ASTRBOT_ROOT`
指向临时目录，不会读写真实实例的数据；全部通过时退出码为 0。

---

## 与 astrbot_plugin_librunner 的关系

[`astrbot_plugin_librunner`](https://github.com/cxykevin/astrbot_plugin_librunner) 是本插件的
第一个注册方：它把自定义 Agent 执行器接进 `agent_runner.runner_type`，并把执行器的
`label` / `description` 与配置分组登记到本插件，于是「执行器下拉框 / 更换执行方式对话框 /
AI 页面的配置区」都不会再出现 `[MISSING: ...]`。

两者的关系是「登记方 ↔ 注入方」，**没有代码依赖**：

| 组合 | 结果 |
| --- | --- |
| 只有 librunner | 执行器能正常使用，面板文案显示 `[MISSING: ...]` |
| 只有 libi18nject | 正常启动，注册表为空，不产生任何影响 |
| 两者都装 | 文案补齐，面板不再出现占位 |

接入方式见[快速开始](#快速开始注入方插件)。

---

## License

[AGPL-3.0](./LICENSE)
