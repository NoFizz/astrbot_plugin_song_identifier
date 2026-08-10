# Changelog

本项目的所有重要变更都会记录在此文件中。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Changed

- 统一插件日志系统：所有日志带 `[听歌识曲]` 前缀；`debug_log` 开关集中管理，媒体转换、引擎尝试、识别结果、输出形式等全步骤均有详细日志。
- 日志来源精确化：`log.debug/info/warning` 携带 `stacklevel`，AstrBot 日志来源从 `astrbot_plugin_song_identifier.log` 指向真实业务文件（`main`/`media`/`engines.acrcloud` 等），便于按日志定位到具体业务代码行。
- 级联识别日志：输出每个引擎的尝试过程（正在使用哪个引擎/返回什么结果/无结果/错误/超时）。
- 引擎级详细日志：四个引擎（ACRCloud/Shazam/讯飞 ACR/讯飞 qbh）均记录请求构建、上传大小、HTTP 状态、响应大小、解析成功/无结果/错误分类。
- 媒体/增强/输出层日志补全：媒体来源分支（语音/视频/文件）、ffmpeg/ffprobe 成败、网易云查询/命中/封面、图片下载与生成、卡片构建。

### Fixed

- 修复 ffprobe 时长探测失败：改用带 key 的 `key=value` 输出并按 key 解析，不再依赖 ffprobe 字段输出顺序（实测 mp4 输出顺序不固定，导致 `float('s16')` 崩溃、时长永远显示未知）。
- 恢复 `debug_log` 开关：重构时丢失了详细日志机制，现已在媒体归一化、识别结果、LLM 工具调用等关键步骤重新接入（受 `advanced.debug_log` 控制）。
- 识别失败/引擎回退时输出各引擎失败原因（provider/mode/错误类型/错误码，脱敏），便于排查"为什么用了兜底引擎"。

### Changed

- 按官方 API 契约重构识别核心：ACRCloud 原生、讯飞 ACRCloud（原声/哼唱）、讯飞 qbh、ShazamIO 四类引擎独立实现，统一错误分类（无结果/配置/鉴权/限流/网络/协议/超时/取消）。
- 原声与哼唱拆分为独立引擎档位：不再隐式"原声失败自动哼唱"，由用户按 首选→次选→备选 自行排列。
- 默认识别片段从 30 秒改为 12 秒：符合 ACRCloud 官方"只处理前 12 秒"的边界，减小上传体积。
- 媒体处理迁移至 AstrBot `data/temp` 目录，识别结束/失败/取消后统一清理临时工件。
- 网易云/QQ 音乐增强与识别核心隔离：增强失败不影响识别结果，平台歌曲 ID 分平台存放（网易云/QQ 不再混用）。
- 日志分级与脱敏：详细日志受 `debug_log` 开关控制，不输出密钥、authorization 与完整响应。

### Added

- 结构化识别错误 `RecognitionError` 与错误分类 `ErrorKind`。
- 总 deadline 级联识别：超时/取消向下传播并清理子进程与临时文件。
- 官方签名 golden vector 测试：ACRCloud HMAC-SHA1、讯飞 HMAC-SHA256 均与官方文档示例逐字节一致。
- 配置契约测试：`_conf_schema.json` 与引擎标签映射一致性、默认值校验、凭据默认值安全检查。

### Removed

- 移除已弃用的 `@register` 装饰器（AstrBot v3.5.19 后自动识别 Star 类）。
- 删除旧版 1475 行单文件 `main.py` 中的重复实现与旧版测试（行为契约已迁移至新模块测试）。
- **配置破坏性变更**：`engines.xfyun_humming` 凭据块已合并进 `engines.xfyun`（三类讯飞引擎共用一组凭据）。升级后请在 WebUI 插件配置中重新保存一次（AstrBot 会自动按新 schema 重建配置）。

### Security

- 凭据仅保存在 AstrBot 本地配置文件，插件代码/日志/README 不含任何密钥。

## [1.0.0] - 2026-08-10

### Added

- 引用语音/视频/音频文件消息识别歌曲。
- 多引擎级联：ACRCloud、Shazam、讯飞开放平台/ACRCloud、讯飞开放平台/自研。
- 网易云歌曲信息增强（封面/试听链接/歌曲 ID）。
- 文本 / 图片卡片 / QQ 音乐卡片三种输出形式。
- LLM 工具 `identify_song`：自然语言触发识别。
