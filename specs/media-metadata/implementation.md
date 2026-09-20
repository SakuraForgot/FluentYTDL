# 元数据实施与验收记录

日期：2026-09-20。用户已授权实施；本次不发布版本、不改写用户已有下载。

## WebM 技术属性显示核查

2026-09-20 最新用户下载 task 1644、flow `32a18c`、run `31a85f`，文件为 `F1 Drivers Race Oldest To Newest Cars.webm`，来源视频 `N-liL_cpEMQ`。日志记录文字元数据 `verified`、最终 `success`、`degraded=false`、`missing=[]`；这只证明列入期望的文字字段，并不承诺 Windows 展示全部技术属性。

对最终文件只读检查：

| 属性 | ffprobe / 成品证据 | 本机 Windows 命名属性 |
| --- | --- | --- |
| 分辨率 | VP9，3840 × 2160 | FrameWidth / FrameHeight 空 |
| 帧率 | r_frame_rate、avg_frame_rate 均为 24000/1001，即约 23.976 fps | FrameRate 空 |
| 总时长 | 1420.581 秒 | Duration 正确 |
| 音频 | Opus，48000 Hz，2 声道 | SampleRate / ChannelCount 空 |
| 总平均码率 | 文件 2648271305 字节 × 8 / 1420.581 秒 ≈ 14.914 Mbit/s，包含容器开销 | 分轨 EncodingBitrate 空 |
| 分轨平均码率 | 当前无 stream.bit_rate，也未生成 BPS 统计标签；不能以总码率冒充视频或音频码率 | 空 |
| 文字作者 | ARTIST=Red Bull，DATE=2026 | Music.Artist 空 |

结论：帧率、尺寸和音频参数在成品中可正确读出；本机 WebM 属性处理器没有向对应 System.* 属性返回值。分轨平均码率统计是目前尚未生成的独立信息，不应将“没有分轨 BPS 标签”和“媒体流参数丢失”混为一谈。本次仅核查，不改编码、不转换容器、不修改用户文件或系统属性处理器。

Windows 属性由按文件类型关联的处理器读取，见 [Microsoft 属性处理器说明](https://learn.microsoft.com/en-us/windows/win32/properties/building-property-handlers)。Matroska 标准包含平均码率 BPS 技术标签，但标签写入不保证所有 reader 使用，见 [标签规范](https://www.matroska.org/technical/tagging.html) 与 [读取优先级说明](https://www.matroska.org/technical/tags-precedence.html)。后续若增加分轨统计，应基于最终媒体包统计并明确计算口径，不能抄网站估算值或仅增加一个名为“帧率”的文本字段。

## 真实视频回归修复：作者等标签缺失

2026-09-20 用户样本：[s3Ci1H80kzU](https://www.youtube.com/watch?v=s3Ci1H80kzU)。下载日志 `flow-abb8f5` / task 1641 表明来源已包含频道作者，但 finalizer 返回 `tool_failed`，只交付原有媒体。时长、分辨率等流属性存在不代表文字元数据写入成功。

对下载文件的副本复现 AtomicParsley `--overWrite` 错误：`insufficient space to retag the source file (1999825!=1997777)`。该文件包含封面和 metadata padding；这是原地更新路径的空间计算问题，不是缺少作者来源或磁盘容量不足。小型 faststart MP4 + 封面 padding + 长简介也复现了同类错误。

修复：MP4 writer 使用明确的 `--output` 直接写入 staging 分配的空候选，源文件全程只读；工具失败或取消后，未采纳输出由 staging 统一清理。writer 不自行移动、删除文件，也无需提前复制整份视频。原生标签、媒体结构、封面/空间数据验证通过后，仍由 staging 原子采纳。没有启用不验证的默认 metadata 回退。

实测已通过完整 finalizer 和 staging 提交：作者 `NTT INDYCAR SERIES`、年份 `2026`、标题和规范来源链接读回正确，所有计划字段 verified；前后音视频及封面流哈希相同。Windows 命名属性也确认作者、年份及来源备注正确。最终修复副本位于 `build/metadata-debug/repaired/s3Ci1H80kzU.metadata-verified.mp4`，原下载文件未覆盖。此证据针对已有真实下载文件的修复链路，不冒充修复后从网络重新下载的全程验收。

新增 faststart/封面 padding/长简介回归，以及独立输出失败和取消保护测试；元数据专项现为 50 项通过，与产物架构约束合计 63 项通过。在独立测试进程恢复旧 `--overWrite` 路径，该回归用例确实失败。此前的冻结应用构建对应首版，尚未为本次小修复重新生成 EXE。

本次修复后全量回归：`1973 passed, 2 skipped`；Ruff、格式和 diff 空白检查通过。两个跳过项仍为首版记录的既有环境/待实现项。

## 已落地行为

- 创建任务时冻结 `accurate_v1` 策略并保存到任务选项。显式关闭优先于全局默认和旧 FFmpegMetadata 配置；恢复旧任务时迁移一次，后续重试沿用快照。
- 关闭 yt-dlp 默认文本元数据和 info-json 内嵌，章节保留独立控制。纯字幕、纯封面不进入媒体标签写入。
- 使用 `after_move` 白名单 JSONL，放在当前 attempt 的内部控制目录；校验身份、路径、行大小及冲突记录，不从文件名猜来源。
- 普通视频使用标题及真实创作者/频道，保留作者角色。仅明确音乐上下文允许采用曲目标题和作品艺术家；播放列表不会变成专辑，分类和关键词不会变成流派。
- 年份只写合法四位值；完整上传日、发行日分别保存。中文、多作者、逗号姓名保留；无效和无来源字段不编造。
- MP4/M4A 使用 AtomicParsley；MP3、FLAC、Vorbis/Opus 使用显式依赖 Mutagen；MKV/MKA/WebM 使用受保护检查约束下的 FFmpeg stream copy。依据实际容器探测选择后端。
- 在封面、字幕、VR 等 Feature 之后、staging 提交之前统一写入。全部写候选，标签回读、媒体结构和受保护内容验证后才原子采纳；写入或校验失败保留原文件。事务安全异常保持硬失败。
- 原有封面、章节、多音轨、字幕、非自有标签以及已识别的 MP4 空间/HDR atom 受保护。运行时不为文字元数据重编码，不把网页时长/码率覆盖到媒体属性。
- 元数据字段期望及采纳事实进入已有观测汇总，使用脱敏 artifact 引用、字段名、来源类别及结果码；失败提示“文件已保存，部分元数据未写入”。UI 中英统一为“嵌入元数据 / Embed metadata”。
- Mutagen 已加入依赖和 lockfile，PyInstaller 收集相关模块；冻结应用自检增加标签标准化及 ID3 写入回读。

## 代码位置

`models/metadata.py` 定义策略、标准化结果和报告；`processing/metadata_policy.py`、`metadata_source.py`、`metadata_normalizer.py`、`metadata_process.py`、`metadata_writers.py`、`metadata_verifier.py`、`metadata_finalizer.py` 分别负责策略、来源、语义、工具运行、写入、回读保护和编排。

下载侧改动位于 `download_manager.py`、`executor.py`、`staging.py`、`features.py`、`workers.py`；入口同步涉及 `quick_opts.py`、`audio_processor.py`、`youtube_service.py`、`yt_dlp_cli.py`。没有增加提交入口，也没有改变既有 Feature 顺序。

## 自动验证证据

开发环境：Windows 11、Python 3.12.12、Mutagen 1.48.1；本地 yt-dlp `2026.08.30.232658`，FFmpeg/ffprobe `N-126405-g9f63b36a26-20260904`。这些是开发工具版本，不代表最终发布组件版本。

| 检查 | 证据 |
| --- | --- |
| 完整 pytest | `1970 passed, 2 skipped`，含 Qt 既有弃用警告 |
| 跳过范围 | 本机不能建立符号链接；既有待实现 `apply_subtitle_delivery` 测试。元数据真实容器测试没有跳过 |
| 元数据测试 | `tests/test_media_metadata.py` 共 47 项，覆盖开关、日期、角色、隐私链接、attempt 来源、错误和取消 |
| 8 种真实容器 | 自主合成 MP4、M4A、MP3、FLAC、Opus、OGG、MKV、WebM；标签回读成功，前后 stream-copy 流哈希相同 |
| 组合媒体 | 纯视频 MP4、M4A/MP3/FLAC/Opus 封面、MP4/MKV 章节及多音轨/字幕、未知 UUID atom；保留既有 ID3v2.4 及无关注释 |
| 多输出及幂等 | 同源视频与音频分别采用相应标题；音乐字段重复写入仍成功；识别 ffprobe 的 track/disc/album_artist 别名 |
| 失败/取消 | 损坏候选、丢失封面、缺来源、预取消和库写入后取消均保留原文件；Worker 写入失败仍提交安全媒体并形成缺失事实 |
| CLI 协议 | 实际 yt-dlp 使用本地 fixture 下载，验证 UTF-8 白名单 JSONL、路径与下载进度同时正常 |
| Windows 命名属性 | 在本机 Shell Property System 对合成样本读取 `System.Title`、`System.Media.Year`、`System.Music.Artist`、`System.Comment`，结果见下表；未操作属性对话框 |
| 静态检查 | Ruff 检查与格式、版本 3.7.3 一致性、lock 一致性、i18n 审计；规则和翻译文件同步 |

最终 `--target spec` 构建及启动自检通过，运行 ID 为 `774f6eaf846e4a60877e4e10957b8638`。证据在 `build/runs/774f6eaf846e4a60877e4e10957b8638/build-result.json` 和 `self-test.json`：冻结状态为 true，Qt、资源、Python.NET、POT 和新增 Mutagen ID3 回读自检均通过。源文件一致性保护也通过，之后仅补录本文结果；没有变更运行代码。`spec` 不生成发布安装包，也不验证在线媒体下载。

构建过程发现当前 PATH 中的 Codex Poppler ICU DLL 会导致 QtCore 导入失败；仅对构建进程过滤 `codex-runtimes` 路径后恢复，不改系统 PATH。另一次中间构建因并行更新翻译/文档被源指纹保护拒绝登记，最终在停止文件修改后完整重跑成功；没有绕过保护。

本机 Windows 属性读回（只代表 Windows 11 10.0.26200 当前属性处理器）：

| 容器 | 标题 | 年份 | 作者 | 备注 |
| --- | --- | --- | --- | --- |
| MP4 / M4A | 中文、emoji 正确 | 2026 | `甲,乙` 与 `丙` 分别显示 | 规范来源 URL |
| MP3 | 正确 | 2026 | 正确 | 不显示自有描述键的来源 COMM；原生标签回读正确 |
| FLAC / Opus / OGG | 正确 | 2026 | 正确 | 当前 handler 显示 DESCRIPTION 内容；COMMENT 来源 URL 在原生标签中独立保留 |
| MKV / WebM | 正确 | 未显示 | 未显示 | 规范来源 URL；日期、作者容器回读正确 |

MP4 的 `System.Keywords` 返回空值，符合本实现仅将来源关键词保存为自有语义字段的边界。以上“未显示”不等于写入丢失，不据此把不同语义合并进备注。

## 实施取舍与边界

- 首版只信任本 attempt 来源。设计允许的“已验证缓存来源回退”没有启用；当前来源失效即保留媒体并提示，不借用旧缓存避免错配。
- 来源/计划采用白名单字典与 `NormalizedMetadata`，没有为只读 set/preserve 流程新增复杂的 MetadataPlan/Source 类；不提供用户手工删除标签。
- MP4 常规短字段超过 255 UTF-8 字节不静默截断，报告限制；简介上限 16 KiB，截断明确标记；Windows 命令行超限安全放弃候选。
- 未确认的音乐上下文仍按普通内容。扩展字段使用自有命名空间，多作者另存 JSON；自有分类/关键词不承诺 Windows 的“标记”栏会显示。
- WAV、裸 AAC 以及无法保真表达的复杂 Matroska 标签结构保留原媒体并报告限制，不换容器、不借用默认标签映射。
- 当前结果详情进入已有诊断事件；没有新增逐字段编辑/预览页面或精简 sidecar 导出开关。未获得来源的提示目前与部分失败共用一条简洁文案。
- 合成样本验证不能替代真实 VR/HDR 设备、完整 SponsorBlock/区间截取、在线网站、登录内容、Windows 属性处理器及播放器的全面兼容验收。发布前须用最终工具组合再验收。

## 回退与后续验收

关闭“嵌入元数据”可禁用新文本 finalizer；已冻结任务按原快照运行。单文件失败自动保留已完成其他后处理的原媒体。回退代码时需要一并回退策略/CLI/finalizer 接线，不能只恢复上游默认写入而保留 finalizer 造成双写。

后续独立验收：真实普通视频、明确音乐作品、频道/列表批量、提取并保留视频、VR/HDR、SponsorBlock 和区间截取；Windows 命名属性与播放器抽验；最终发布 EXE 和随包组件完成同样媒体流程。本次未发布、未推送。
