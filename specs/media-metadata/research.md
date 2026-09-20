# 音视频元数据嵌入：现状研究与方案建议

日期：2026-09-19。范围：FluentYTDL；本轮为研究和规划，不修改下载行为。

**范围修正：用户已确认截图来自其他软件，只参考展示字段。后续以 [design.md](design.md) 为当前方案；不再诊断该截图的异常。以下初轮猜测不构成本项目缺陷结论。**

## 1. 证据与边界

- 仓库基线：`c441f2c`，工作区存在用户正在进行的翻译、诊断和播放列表修改；本研究未改动这些文件。
- 已核查：任务入口、Feature 链、CLI 参数翻译、产物事务、封面处理、上游文档及源码。
- 本地开发目录工具自报：yt-dlp `2026.08.30.232658`；FFmpeg/ffprobe `N-126405-g9f63b36a26-20260904`。本地配置的两个工具路径覆盖项为空；不将这些版本推定为用户已安装 EXE 的版本。
- 上游源码依据是研究时的 `master`，不是本地 nightly 对应提交，具体行为仍须用实际执行的二进制验收。
- 输入截图来自其他软件，用作字段清单参考；无需提供其 MP4 来推进本项目设计。
- 重试时基础 Shell COM 创建成功，但完整合成媒体及属性回读命令被审批策略拦截；不宣称容器或 Windows 属性验收通过。该历史实验不再作为设计前置条件。

## 2. 截图逐项判断

| 截图内容 | 判断 | 处理方向 |
| --- | --- | --- |
| 备注为 YouTube URL | 与上游默认 `webpage_url → comment` 一致 | 保留可回溯的规范页面链接；简介独立存储 |
| 年 `10292` | 参考软件显示值；不属于本项目缺陷证据 | 本项目自主验证日期和年份，不追查参考软件根因 |
| 艺术家为多个 ESPN 名称 | 可能来自 artist、creator 或 uploader；截图无法确定 | 保存来源字段；区分作品艺术家与上传频道 |
| 流派 `Sports` | 很可能是网站分类，但不能仅凭截图确认 | 分类、关键词和艺术流派独立建模 |
| 分级与标记为空 | 不代表下载失败；可能无来源、未映射或 Shell 不支持 | 不根据点赞数生成星级，不强填所有属性栏 |
| 时长、宽高、帧率、码率、声道、采样率 | 属于最终音视频流的技术属性 | 从成品探测，不从网页估算值写成文本标签 |

初轮曾根据数值提出日期截断猜测，未经验证。用户明确范围后不再沿此方向调查；它不进入实现逻辑。设计只保留普适要求：来源完整日期、作品发行年及容器显示年份有清晰区分。[上游映射](https://github.com/yt-dlp/yt-dlp#modifying-metadata)、[Windows 年份属性](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-media-year)。

## 3. 当前实现及缺口

| 位置 | 现有职责 | 规划需要处理的缺口 |
| --- | --- | --- |
| `download/features.py:167` | MetadataFeature 根据全局设置追加 FFmpegMetadata | 没有字段校验、明确的任务关闭优先级或标签回读 |
| `youtube/yt_dlp_cli.py:866`、`:906` | addmetadata / FFmpegMetadata 翻译为 CLI 开关 | 没有 parse-metadata 映射；False 不产生明确关闭参数；PP 详细配置未透传 |
| `youtube/youtube_service.py:707` | 全局设置生成元数据后处理器 | 多处写入意图，需要与任务快照统一 |
| `utils/quick_opts.py:52` | 快速下载开启时添加两种元数据标志 | 关闭时主要靠缺省表达，后续 Feature 仍可按全局值补回 |
| `ui/components/dialogs/download_config_window.py:4145`、`:4581` | 单项、列表任务写 addmetadata 布尔值 | 显式 False 仍可能与已有 PP / 全局 Feature 冲突；需回归证明 |
| `processing/audio_processor.py:242` | 音频预设追加 FFmpegMetadata | 必须纳入同一意图解析，避免预设反向覆盖任务 |
| `download/executor.py:421` | after_move 文件记录成品路径 | 可以借鉴这种控制文件通道，当前未建立可靠元数据快照输出 |
| `download/workers.py:678`、`:1561` | 固定 Feature 顺序；封面与 VR 在后 | 元数据回读应在全部可能改写媒体的步骤之后 |
| `observability/artifacts.py:124` | 有字幕、封面嵌入事实 token | 尚无逐字段元数据写入证据；PP 完成不等于字段正确 |
| `download/staging.py` | 沙盒、候选产物、替换与提交 | 必须复用，不能直接覆盖用户成品或再次扫描下载目录 |

本地源码支持以上风险判断；没有在本轮运行 GUI 下载来复现开关冲突。`VideoInfo.raw_info` 和 Worker.cached_info 可供参考，但不能假定它们永远包含最新、完整的音轨、章节和音乐信息。

## 4. 建议的数据模型与映射

核心原则：有可靠来源才写；明确空值不等于缺省；一个字段一个语义。标准化记录保留 `value / source_field / semantic_role / validation_status`，记录策略版本，避免只留下最终字符串而失去追溯依据。

优先级建议：用户明确覆盖（含明确清空）→ 本次完整提取的相同语义字段 → 任务已有可信快照 → 缺省。用户覆盖界面属于后续增强；首版仍建立该数据边界。冲突优先选本次同一视频的有效值，不混合不同条目。

| 字段 | 推荐来源与规则 | 优先级 |
| --- | --- | --- |
| 视频标题 | 原始 title，不写清理后的文件名；视频不因背景音乐 track 改名 | P0 |
| 音频标题 | 确认是音乐作品时使用 track，否则 title；“导出音频”本身不等于音乐作品 | P0 |
| 作者 / 频道 | creator、channel、uploader 按语义保存，不合并成虚构共同作者 | P0 |
| 艺术家 | 结构化 artist / artists；普通视频若用频道兼容显示，须标注“频道作为作者”，与音乐艺术家分开 | P0 |
| 年份 | 音乐优先有效作品发行年；普通视频优先有效发布日期，再回退上传日年份；保留来源标记 | P0 |
| 完整日期 | upload_date、release_date 分开；严格验证真实日历日期，保留 YYYY-MM-DD；只有年份就不补造月日 | P0 |
| 来源链接 | 规范 webpage_url；缺失时仅使用可验证的页面链接；不嵌入 CDN 签名直链 | P0 |
| 简介 | description；保留合理换行，清除 NUL；按格式限制裁剪并记录，不挤占来源备注 | P0 |
| 分类 | categories；普通视频分类独立保存，不默认伪装成音乐流派 | P1 |
| 关键词 / 标记 | tags；顺序稳定地去重；多值在标准化层保留列表，由容器决定编码 | P1 |
| 流派 | genre / genres；音乐不以任意 tags 或 Sports 分类兜底 | P1 |
| 专辑 / 专辑艺术家 | album、album_artist(s)；播放列表不默认当专辑 | P1 |
| 曲目号 / 碟号 | track_number、disc_number 及可靠 total；不默认使用 playlist_index | P1 |
| 作曲者、版权、许可证 | 明确的 composer、copyright、license；许可证与版权所有者不混为一谈 | P1 |
| 节目 / 季 / 集 | series、season_number、episode_number 等对应字段，缺失留空 | P1 |
| 来源 ID / 频道 ID / 提取器 | 容器扩展标签或用户选择的精简 sidecar，用于追溯 | P1 |
| 音轨 / 字幕语言 | 实际选中的流、字幕轨；复用项目 BCP-47 规则，按容器要求转换 | P1 |
| 章节 | 合并、区间截取及 SponsorBlock 处理后的最终时间轴；不是直接复制原始章节 | P1 |
| 封面 | 复用 ThumbnailFeature；与文本元数据开关独立；验证后续改写不丢图 | P0 保留 |
| 星级、BPM、歌词、ISRC、地理信息 | 仅有明确可信来源或用户输入时考虑，不根据点赞、标题或画面猜测 | P2 |

完整日期不写入文件系统创建/修改时间来冒充作品日期。上传时间也不自动冒充拍摄时间、录制时间。原始缩略图生成、VR 空间属性和文本标签是不同处理职责。

格式规范化建议：Unicode 原样保留，不对艺术家名单按逗号盲拆；日期用日历解析；序号必须为正整数；未知值、`NA`、非法日期与空对象不写。年份基础范围为 1–9999，并对与来源上下文明显冲突的值发出信号，不能把未来发行日期一律当错误。

## 5. 容器兼容策略（候选，待实验冻结）

不能承诺“每个格式都能在 Windows 属性页展示相同字段”。能力表需分为：可写入、可回读、Windows 可见、目标播放器可见。

| 容器 | 首选候选 | 特别验收 |
| --- | --- | --- |
| MP4 / M4A | 优先现有 yt-dlp + FFmpeg 标准标签；精确 atom 修正仅在实验证明必要时引入现有工具 | 标题、艺术家、年份、备注；完整日期与年份分开；封面、字幕、章节保留 |
| MP3 | ID3；优先比较现有 FFmpeg 写入与 ID3v2.3 / 2.4 兼容性 | 中文、多艺术家、COMM、年份、APIC；不能仅靠 ID3v1 承载长文本和中文 |
| FLAC | Vorbis Comment + 图片块 | 日期、重复多值、封面；播放器与 Shell 分开验收 |
| OGG / Opus | 对应 comments 机制 | 标签、音轨属性和播放器兼容；不宣称 Windows 全可见 |
| MKV / MKA | Matroska Tags | 标签 target 层级、章节、附件、字幕、音轨均保留 |
| WebM | 依据实际 muxer、工具与播放器验证字段子集 | 不直接套用完整 MKV 能力表 |
| WAV / AAC 裸流及其他格式 | 先界定有限能力；必要时用户选择精简 sidecar | 不静默换容器，不以改后缀冒充嵌入成功 |

MP4 的 `©nam / ©ART / ©day / ©cmt` 等字段与任意自定义键不是同一套兼容承诺；`keyw` 在 Mutagen 文档中是 podcast keywords，并不保证映射为资源管理器的“标记”。`use_metadata_tags` 会使用 mdta atom，不能为了容纳更多键就全局开启。[MP4 标签文档](https://mutagen.readthedocs.io/en/latest/api/mp4.html)、[FFmpeg muxer 文档](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv)、[Windows 标记属性](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-keywords)。

## 6. 初轮候选接入方案（当前决策见 design.md）

建议先保持 yt-dlp CLI 作为下载引擎，增加确定性的意图、映射与回读层。不要直接将 yt-dlp Python API 接入项目；也不要为了几个标签先引入整套音乐识别服务。

1. **任务意图唯一入口**：形成 `MetadataPolicy` 快照，任务显式值优先，全局只作默认。统一清理冲突开关；关闭输出 `--no-embed-metadata`，元数据、章节、info-json 附件分别控制，不把停用文本标签误解为删除原有标签。
2. **来源快照**：从本次实际 yt-dlp 进程输出白名单 JSON 到 staging 内部控制区，携带视频 ID、attempt、artifact 关联；可借鉴现有 print-to-file，不启用会压制进度输出的全局 print，也不把完整 info.json 默认交付给用户。导出时机和 JSON 模板必须先做 CLI 实验。
3. **映射与验证**：仅写元数据目标字段，避免改原始 title 影响文件命名，避免改章节源数据干扰 SponsorBlock。简单的 meta_* 映射可通过 CLI 转译；复杂日期/角色规则只有拿到可靠快照后才生成。
4. **写入方式选择门**：先验证 CLI 映射能否实现核心字段；若无法可靠传入经过 Python 校验的值，则选择事务内的独立 writer，并关闭上游相同文本字段写入。每个字段只保留一个最终写入者，避免默认写一遍再无条件重封装一遍。
5. **候选文件与采纳**：独立 writer 只读主媒体，在 reserve_workfile 中输出候选；保留原有未知标签、字幕、章节、图片与空间数据；验证后通过 replace_artifact_content 采纳。用参数数组调用工具，避免长简介挤爆 Windows 命令行，必要时使用经过转义的受控元数据文件。
6. **最终复核**：所有封面、VR、裁剪等写入完成后再读实际成品。首选在既有 Feature 顺序之外增加统一最终检查，而不是未经评估改动 AGENTS 规定的顺序。若必须调整顺序，再同步规则源和相关测试。
7. **结果表达**：区分已写入并验证、无源字段、格式不支持、验证失败。只有用户要求且有来源且预期受支持的字段写入失败，才形成 expected − actual 的缺失；不新增事件种类、不产生第二个 outcome、不把字段正确性塞入 staging 的二值安全门。

候选组件为无状态 `MetadataPolicy`、`NormalizedMetadata`、容器能力表、writer、verifier；命名和文件布局留待需求确认后的正式设计，不新增全局 singleton。

失败策略：候选无效时保留旧的可播放主媒体，丢弃未登记候选并提示元数据不完整；输出记录具体字段/阶段，不笼统显示网络错误。安全门发现路径逃逸、主媒体损坏等仍应阻止提交。重试不得复用旧 attempt 的 JSON。关闭嵌入是“不新增修改”，不是抹去源文件已有标签。

## 7. 建议实施顺序

| 阶段 | 工作 | 完成门槛 |
| --- | --- | --- |
| P0-A | 本项目自主日期、艺术家、多值、开关样本 | 验证既定字段语义及容器编码，不依赖参考软件 |
| P0-B | 单任务意图、核心映射、验证与失败回退 | 快速/精细/列表均遵守任务值；标题、作者角色、年份、来源准确 |
| P1 | MP3 / FLAC / Opus / MKV 扩展及关键词、专辑、章节等 | 按能力矩阵回读，组合处理不丢字幕、封面、VR 数据 |
| P2 | 字段预览、手动覆盖、精简 sidecar、显式旧文件修复入口 | 用户可看到来源与不支持原因，旧文件修改独立授权并可回滚 |

首版不建议自动联网猜歌曲、不扫描已有下载目录批量改写、不自动给视频评分。界面先统一“嵌入元数据”措辞，与“保存元数据文件”区分；不先堆出几十个默认勾选项。

## 8. 参考资料

- [yt-dlp 元数据覆盖与默认字段](https://github.com/yt-dlp/yt-dlp#modifying-metadata)
- [yt-dlp FFmpegMetadataPP 源码](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/postprocessor/ffmpeg.py)
- [Microsoft System.Media.Year](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-media-year)
- [Microsoft System.Music.Artist](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-music-artist)
- [Microsoft System.Keywords](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-keywords)
- [FFmpeg 容器文档](https://ffmpeg.org/ffmpeg-formats.html)
- [Mutagen MP4](https://mutagen.readthedocs.io/en/latest/api/mp4.html)、[ID3 版本与编码](https://mutagen.readthedocs.io/en/latest/user/id3.html)
- [Matroska 标签规范](https://www.matroska.org/technical/tagging.html)
