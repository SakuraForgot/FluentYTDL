# 03 · 覆盖矩阵与纵向切片

## 1. 使用方法

本文件是 [RECOMMENDATION] 验收清单，不是“全部已调查完”的声明。真实首轮结论见三个 [evidence 报告](README.md)。每个覆盖项逐步经历 `未开始 → 已定位 → 已取证 → 已复核`；动态验证作为独立列，不能由“已复核”自动推导。

## 2. 模块候选覆盖

下列是基于当前源码区域的调查分组；最终逻辑边界须补调用关系后由 Lead 固化。

| ID | 区域/候选模块 | 必须追踪的关系 | 主要章节 | 首轮证据 |
| --- | --- | --- | --- | --- |
| M01 | 根 main、启动与主窗口 | 特殊参数→路径环境→Qt 初始化→服务→关闭 | 01/03/09/12 | Repository、Runtime |
| M02 | 六解析页、配置窗口、extract workers、YoutubeService | URL→模式→metadata→options→task；用户取消、缓存代际 | 02/03/04/05 | Runtime |
| M03 | PlaylistScheduler、AsyncExtractManager、列表模型 | 懒加载/优先级→解析槽→回调→模型脏行；过期回调 | 03/05/09 | Runtime |
| M04 | AppController、DownloadManager、DownloadWorker | 增删/暂停/恢复/补槽→状态持久化→UI | 03/06/09 | Runtime |
| M05 | Executor、output parser、质量检查 | 真实工具选择→argv/env→管道→进度/退出→产物判断 | 03/04/08/10 | Runtime、Resource |
| M06 | StagingArea、Manifest、Feature 管道 | 发现→冻结→变换→验证→预留→提交→补偿 | 04/06/07/10 | Runtime |
| M07 | processing、audio/VR/字幕/封面 | 配置意图→格式/语言/容器约束→外部工具→实际交付 | 02/04/08/10 | Runtime，内部细节待深入 |
| M08 | AuthService、CookieSentinel、WebView2、runfile | 账号/平台→验证→真相源→请求副本→清理 | 04/07/09/11 | Resource |
| M09 | POTManager、ytdlp_runtime、DependencyManager | 自管/managed/PATH→版本身份→插件/JS→进程所有权 | 07/08/09/12 | Resource |
| M10 | ConfigManager、TaskDB、DBWriter、paths | 当前值/请求快照/持久行/批量写/迁移 | 04/06/07 | Resource |
| M11 | diagnostics、observability、logger、log viewer | stderr→主因/伴随信号→事件→脱敏→存储/展示 | 04/08/10/11 | Runtime、Resource |
| M12 | updater/worker、component update、update transport | 元数据→渠道→包校验→安装替换→启动确认/回滚 | 03/07/10/12 | Repository、Resource |
| M13 | notification、announcement、i18n、UI 模型 | 公告缓存/已读→展示；结构化事件与本地化分离 | 04/08/11 | Repository；UI 细节待深入 |
| M14 | build/release/spec/Inno/maintenance | 构建快照→目标包→安装/发布→升级/卸载不同数据策略 | 01/08/12 | Repository |
| M15 | ControlCenter admin/worker/shared/migrations | 管理鉴权→D1 草稿/修订→KV 发布→公开客户端读取 | 02/04/09/11/12 | Repository |
| M16 | 测试、规则生成、第三方内嵌代码 | 契约测试指向实现；规范意图与现状冲突；许可证和来源 | 01/08/13/14 | Repository；全量映射待展开 |

## 3. 20 条纵向切片

每条切片至少包含：触发、用户可见行为、代码调用链、数据、状态/写入者、资源、线程/进程边界、正常终点、失败/取消/重试、控制回环、证据与未知项。代码定位种子不是已证完整调用链。

| ID | 用户动作/入口 | 定位种子 | 特别要回答的问题与缘由 |
| --- | --- | --- | --- |
| VS-01 | 启动应用及退出 | `main.py`、MainWindow、DownloadManager.shutdown | 单实例与更新参数先后；后台启动/关闭顺序；超时后是否仍有进程或未完成提交 |
| VS-02 | 解析普通视频并下载 | parse_page、InfoExtractWorker、DownloadConfigWindow | parse 到 select 未有 task_id，如何关联 flow；选择格式何时冻结 |
| VS-03 | 解析 VR 并转换 | vr_parse_page、VRInfoExtractWorker、VRFeature | android_vr 分支、EAC 转换与空间元数据如何改变实际输出和成功判定 |
| VS-04 | 打开频道并批量添加 | channel_parse_page、ChannelExtractWorker、PlaylistScheduler | 频道 tab、分块、可见区优先；取消如何释放提取槽，避免排队停住 |
| VS-05 | 解析播放列表并延迟提取详情 | extract_playlist_flat、EntryDetailWorker、playlist_model | flat 列表与详情请求区别；authcheck 恢复何时允许，避免误判 cookie/private |
| VS-06 | 仅下载字幕 | subtitle_download_page、SubtitleFeature、字幕语言解析 | 轻量信息与语言别名如何对应真实字幕键；空输出不能因进程退出零而成功 |
| VS-07 | 仅下载封面 | cover_download_page、封面直链通道、StagingArea | 直链与轻量 yt-dlp 分支是否都经过事务提交；不要套用旧文档的绕过沙箱说法 |
| VS-08 | 快速添加/片段/音频选择 | quick_add_panel、quick_add_worker、section_download、audio picker | 快捷路径是否保存同样的请求选项；精确片段并发限制的归属与目的 |
| VS-09 | 暂停、继续、手动重试 | controller、WorkerState、DownloadManager | 活 worker 暂停/恢复保留 run，结束后重建 worker 才是新执行；task 保持，文件怎么续用 |
| VS-10 | 自动恢复与自动重试 | Executor、diagnostics、Worker、TaskTrace | 403/429/POT/格式错误分别做什么；最大次数/退避/取消；attempt 不等于新 run |
| VS-11 | 取消/删除任务与重启恢复 | controller 删除回调、staging finalize、load_unfinished_tasks | commit 前后取消不同；已交付用户文件、暂存文件和数据库记录分别怎样处理 |
| VS-12 | 登录/导入/刷新账号 | AuthService、WebView2CookieProvider、CookieSentinel | 运行时缺失、UAC、子进程早死、平台隔离、验证失败保留旧材料 |
| VS-13 | 切换 Cookie 模式与账号 | youtube_request、parse cache、cookie_runfile | 旧请求是否写回新缓存；排队任务是否受设置变化；匿名模式是否从合并项漏附 Cookie |
| VS-14 | 启用 POT 或更换下载工具 | POTManager、ytdlp_runtime、插件同步 | 真正执行的 exe 与设置显示一致吗；端口所属/Job 管理与共享服务清理界限 |
| VS-15 | 查看日志、错误详情和导出诊断 | log viewer、diagnostics.collect、observability.bundle | 主因与成功信号如何区别；脱敏在原始输出、UI、JSONL 和导出是否都适用 |
| VS-16 | 检查/安装应用更新 | app update card、update_transport、updater、update_signal | stable/pre、manifest、校验、换源、替换、降权重启、ready token 与回滚 |
| VS-17 | 更新 yt-dlp/FFmpeg 等组件 | component_update_manager、DependencyManager | 组件状态/工具实际路径是否一致；自定义/PATH 工具不能被误当 managed 覆盖 |
| VS-18 | 读取公告及后台发布公告 | announcement_service、ControlCenter Worker/Admin | 草稿→修订→发布快照→客户端缓存→已读；KV 写失败怎样重试而不丢失修订 |
| VS-19 | 安装、便携启动、覆盖安装、卸载 | Inno、paths、install_registration、maintenance | LocalAppData 与 app/bin/dle_user 等根的区别；保留媒体/未知文件、清除应用隐私数据 |
| VS-20 | 构建并发布一个版本 | build.py、component_snapshot、release_pipeline、release.yml | 锁定环境/最新工具快照；artifact target；草稿验证→公开字节→服务同步 |

VS-20 的用户是维护者；其操作不应伪装成桌面运行时的一部分。ControlCenter 的 Admin 操作同理。

## 4. 横切检查表

| 横切主题 | 至少关联切片 | 必须收集 |
| --- | --- | --- |
| 身份 | 02/05/09/10/11/15 | session、flow、task、run、attempt、transaction 的创建点、延续/替换条件 |
| 状态 | 01/09/10/11/16 | Worker 状态、DB status、UI 派生状态、事务 phase、run outcome、updater 状态分别列写者 |
| 文件资源 | 06/07/11/13/16/19 | payload、parts、manifest/journal、用户交付物、Cookie/runfile、配置/DB/日志、安装备份 |
| 进程/线程 | 01/04/10/12/14/16 | Qt 主线程、QThread、ThreadPoolExecutor、TaskDBWriter/threading.Thread、yt-dlp/FFmpeg/POT Provider EXE/JS runtime/WebView2/updater |
| 控制回环 | 04/05/10/12/14/16/18 | trigger、repeat、backoff/timeout、取消传播、持锁区间、资源变化、退出条件 |
| 错误与安全 | 全部 | 原因来源→分类→决策→UI/持久化，外部输入校验、日志脱敏、边界内外所有权 |

额外普查关键词包括 `Popen`、`subprocess.run`、`multiprocessing`、`QThread`、`ThreadPoolExecutor`、`threading`、`QTimer`、`.connect`、`.wait`、`while`、`retry`、`Lock`、`sqlite3`、`os.replace`、`unlink`、`rmtree`、`write_text`、环境变量及动态 imports。搜索命中只是候选，不代表存在控制回环或错误。

## 5. 不适用项的处置

原始提示词中的 Rust crate、Tauri IPC、Steam Manifest、后端 WebSocket 不自动套入本项目。若全范围搜索和调用调查没有找到，登记“不适用/未发现＋搜索范围”，而非创造空模块。本项目的 artifact Manifest、更新包 manifest、SQLite 与 ControlCenter D1/KV 要各自建实体，名字相似不能合并。
