# 源码依据与技术选择

## 当前代码核查

| 当前实现 | 对新页面的影响 |
| --- | --- |
| [主窗口页面初始化与导航](../../src/fluentytdl/ui/reimagined_main_window.py)，页面初始化约 226 行、`init_navigation()` 约 378 行 | 页面通过 `addSubInterface()` 注册；“设置”固定底部，“任务”维护自己的下载徽标。新增页面不参与任务计数 |
| [主窗口退出](../../src/fluentytdl/ui/reimagined_main_window.py)，`closeEvent()` / `quit_app()` | 当前显式停止主题监听与下载管理；需为媒体读取增加独立退出清理，不能只依靠下载管理器 |
| [封面页](../../src/fluentytdl/ui/cover_download_page.py) | 可参考 Signal 交互与 Fluent 控件，不能直接复制其固定宽度的居中输入布局来承载多轨信息 |
| [探测与验证](../../src/fluentytdl/processing/metadata_verifier.py)，`probe_media()` | 已有 FFprobe JSON 探测，包含 format/streams/chapters。还包含写入保真所用的 extradata hash；新查看页不需要默认执行这些验证 |
| 同文件 `container_kind()` | 是写入支持范围判断，并带有后缀条件；不能作为查看器的格式准入表。能够读取的 WAV/MOV 等不应因写入器不支持而整页失败 |
| [现有标签适配器](../../src/fluentytdl/processing/metadata_writers.py) | `read_mp4/read_id3/read_comments/read_matroska` 按传入的预期字段提取，部分会取首值或合并；不适合直接充当完整标签枚举接口 |
| 同文件 `mp4_tags()` | 直接读取 `moov/udta/meta/ilst`，支持纯视频 MP4；可提取为只读公共能力，保持写入回归兼容 |
| [外部工具执行](../../src/fluentytdl/processing/metadata_process.py) | 已支持取消、超时、隐藏控制台和清洁环境；`communicate()` 仍会累计输出，新查看器需要独立的有界输出读取 |
| [工具定位](../../src/fluentytdl/utils/paths.py)，`locate_runtime_tool()`；[配置](../../src/fluentytdl/core/config_manager.py) 的 `ffmpeg_path` | 复用包内工具路径体系；明确自定义 FFmpeg 旁的 FFprobe 选择及失败原因，不在 UI 自行硬编码开发机路径 |
| [程序入口](../../main.py) | 已有早于 GUI 的 `--build-self-test` 模式。原生标签读取辅助进程可采用同类早期入口，避免启动第二个主窗口或服务 |
| [元数据模型](../../src/fluentytdl/models/metadata.py) | `MetadataReport` 表达下载写入的 expected/actual 差集；查看文件没有写入期望，须使用独立 InspectionResult |
| [任务列表菜单](../../src/fluentytdl/ui/unified_task_list_page.py)，`_menu_scope()` / `_on_context_menu()` | 已区分单行与勾选批次，并用延迟解析及 QPersistentModelIndex 抵御插行；新增右键入口应沿用这些语义 |
| [任务行模型](../../src/fluentytdl/ui/models/task_row.py)，`db_id` / `effective_state` / `effective_output_path` | `db_id` 提供稳定身份，状态与路径兼容实时 worker 和历史快照；不必依赖 worker 存在或从标题重建路径 |
| [成品存在性检查](../../src/fluentytdl/ui/models/download_list_model.py)，`_probe_rows()` | 已在后台检查完成行，`file_exists` 可以未知或过期；菜单不做同步磁盘访问，实际识别仍需重新校验 |
| 主窗口 `on_open_target_folder()` | 现有打开文件夹允许退回下载目录，但媒体信息识别不能复制这个目录回退，否则无法保证读取的是该任务成品 |
| [既有实施记录](../media-metadata/implementation.md) | 区分原生回读与 Windows 属性显示，已记录 MP4 和 WebM 案例；新页面不能把 Windows Shell 当唯一读取来源 |

源码行号为本轮阅读定位提示，可能随实施变化；链接以文件为准。

## 外部资料及设计推论

- [FFprobe 官方文档](https://ffmpeg.org/ffprobe.html)：支持分别输出容器、媒体流、章节及其中标签，并提供 JSON 输出、帧/包计数选项。设计据此选择 FFprobe 作为技术信息主读取器，但首版不自动启用全文件计数。
- [FFmpeg AVStream 定义](https://ffmpeg.org/doxygen/trunk/structAVStream.html)：`avg_frame_rate` 与 `r_frame_rate` 语义不同，后者包含推测性质。设计分别展示其来源，不把两个数相等当作恒定帧率证明。
- [Mutagen MP4 文档](https://mutagen.readthedocs.io/en/latest/api/mp4.html)：常规 MP4 信息类面向音轨，标签可含多值及 freeform 字段。设计保留项目已有的纯视频标签读取路径，不用 `MP4().info` 代替视频参数探测。

以上资料证明接口及字段语义，不证明新页面已能运行。字段优先级、读取预算、页面结构是本方案的设计决策，须由后续测试验证。

## 选择与取舍

1. **FFprobe + 原生标签补充**：现有包内组件与 Mutagen 已具备基础，不新增 MediaInfo DLL/CLI 依赖。暂不承诺 MediaInfo 的全部字段覆盖。
2. **独立查看服务**：不调用 `MetadataFinalizer`、`normalize_metadata()` 或下载来源回填规则，防止把观察值变成“应该写入的值”。
3. **原生标签枚举保留多值和作用域**：已知字段映射为中文/英文名称，未知键在原始读取结果中保留；非文本二进制仅显示类型和长度。
4. **主进程不运行长时间标签解析**：FFprobe 与原生标签辅助进程依次执行，由后台作业管理。这个额外入口用于超时和取消隔离，不增加第二个常驻服务。
5. **深度统计后置**：逐轨包字节统计和解码帧数属于扫描操作，不能伪装成即时属性读取；后续独立设计后再加入。

## 文档验证边界

本轮仅做源码阅读、官方接口核查及文档检查。没有实现 UI、运行新读取链路、打包或发布；布局尺寸与性能预算均为目标，不能表述为实测结论。
