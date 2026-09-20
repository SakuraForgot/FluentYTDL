# 资源、数据、并发、安全与恢复：源码证据底稿

调查日期：2026-09-19。仅静态阅读当前工作区源码与测试文件名/断言入口；未运行产品、下载、认证、更新、测试，也未读取真实配置、Cookie 或数据库内容。完整阅读根目录 AGENTS.md。下文相对链接从本文件出发，`#L` 给出调查时行号；后续改动可能移动行号，应以符号名定位。

证据分级：`[CONFIRMED]` 为源码可直接验证的行为；`[INFERRED]` 为据该行为推导的工程作用；`[UNKNOWN]` 为本轮尚未证明的运行事实；`[RECOMMENDATION]` 为后续方案或验收建议。源码注释明确记录的动机，会说明其为源码设计说明，不能替代运行验收。

## 1. 持久数据不是一个根目录

| 资源 | 创建/持有与读写 | 释放、异常与跨启动 | 处理缘由及证据 |
|---|---|---|---|
| 配置、任务 DB、日志根 | [CONFIRMED] `user_data_dir()` 优先环境变量覆盖，其次 frozen+portable.txt 的 exe 目录，再其次 LocalAppData；开发模式为项目根。 | [CONFIRMED] 函数尝试建目录，OSError 被吞掉；返回路径并不等于验证了可写。 | [CONFIRMED] 源码说明禁止靠写权限决定位置，以免提权与普通会话落入两棵数据树。[paths.py::user_data_dir](../../../../src/fluentytdl/utils/paths.py#L81) |
| config.json | [CONFIRMED] ConfigManager 内存字典由 `_load_config` 构建；`set` 修改字典→save→configChanged。 | [CONFIRMED] `save` 直接 write_text，捕获全部异常；不存在该函数层面的临时文件+replace 提交。 | [INFERRED] 吞错保护 UI 可用性，但会出现内存/磁盘不同步；不能将它描述为可靠事务配置存储。[ConfigManager.save/set](../../../../src/fluentytdl/core/config_manager.py#L461) |
| SQLite tasks.db | [CONFIRMED] TaskDB 单例在 `_init_db` 建立共享连接；路径是数据根/state/tasks/tasks.db；显式 db_path 创建独立实例供测试。 | [CONFIRMED] WAL、synchronous=NORMAL、check_same_thread=False；旧路径迁移后继续使用统一路径。 | [INFERRED] 写锁负责进程内串行写，WAL 缓和读写互斥；连接模型与独立 TaskDBWriter 线程同时存在。[TaskDB._init_db](../../../../src/fluentytdl/storage/task_db.py#L67) |
| TaskDBWriter 队列/线程 | [CONFIRMED] `TaskDBWriter.__init__` 创建 Queue 及 daemon Thread 并启动，enqueue 接收请求，后台 `_run` 收集批次后调用 TaskDB。 | [CONFIRMED] `flush_and_stop` 投递 None，消费者 drain 并退出；调用者 join(timeout)，没有进一步检查存活或返回成功标志。 | [INFERRED] 将高频状态 I/O 移出 UI，但入队不是提交确认，超时返回不证明全部落盘。[创建线程](../../../../src/fluentytdl/storage/db_writer.py#L40)、[停止与消费](../../../../src/fluentytdl/storage/db_writer.py#L74) |
| 认证配置/来源缓存 | [CONFIRMED] AuthService.cache_dir 默认系统临时目录/fluentytdl_auth，保存 auth_config.json、profiles.json；账号目录为运行 bin/dle_user。 | [CONFIRMED] 初始化加载认证配置、账户、旧缓存迁移、平台目录迁移，校验 youtube/twitter 当前账户。 | [INFERRED] 认证数据不能仅按 user_data_dir 备份或卸载，必须单列临时认证根、runtime bin、账户根。[AuthService.__init__](../../../../src/fluentytdl/auth/auth_service.py#L310) |
| 账户数据与 SABR 标记 | [CONFIRMED] accounts.json 位于 bin/dle_user；已有账户的 sabr_only 写入账户，无当前账号时为内存 session sticky 标记。 | [CONFIRMED] session 标记重启清零，账户标记可持久化。 | [INFERRED] 把网站实验状态绑到实际账户，避免一个账户的兼容策略传播到全部账号。[AuthService.get_youtube_sabr_only/mark_youtube_sabr_only](../../../../src/fluentytdl/auth/auth_service.py#L415) |

[UNKNOWN] 本轮未检查真实安装路径、portable.txt、用户覆盖变量及认证目录 ACL，不能声称当前机器具体数据实际位于上述哪一分支。`ConfigManager._init` 的旧 Documents 注释与 `user_data_dir` 已不同，应以执行代码为准。

## 2. TaskDB 的事务模型与状态恢复

[CONFIRMED] `_writing()` 在非 batch 环境持有 `_write_lock` 并提交；`batch()` 用 thread-local depth 判断当前线程是否已在事务中，最外层 BEGIN、异常 rollback、正常 commit、finally 清深度。源码明确记录：如果事务深度共享，线程 B 的写可能混入线程 A 的事务并被 A 回滚；重复获取不可重入锁则会自死锁。[TaskDB._writing/batch](../../../../src/fluentytdl/storage/task_db.py#L186)

[CONFIRMED] 创建任务会保存 URL、queued 状态及 `ydl_opts_json`；结果、质量、options、run 身份分别有更新入口。它保存的是恢复与展示所需状态，不是所有媒体字节的事务账本。[TaskDB.insert_task](../../../../src/fluentytdl/storage/task_db.py#L227)、[TaskDB.update_task_run_identity](../../../../src/fluentytdl/storage/task_db.py#L323)

[CONFIRMED] 启动恢复复用 tasks.id，并产生恢复审计；恢复后的 worker 可处于 error 而没有实际 start。恢复末尾扫描配置下载目录与任务各自目录的 staging 残留，并清理 Cookie runfile。[DownloadManager 恢复逻辑](../../../../src/fluentytdl/download/download_manager.py#L183)、[启动 GC](../../../../src/fluentytdl/download/download_manager.py#L293)

[INFERRED] task 身份必须与 run 身份分开：同一任务跨启动复用 ID 不能让旧执行残留误判为正在使用；必须把“排队后重启”与“运行中断”区分，否则失败率和恢复语义都会被污染。

[UNKNOWN] 本轮未发现 TaskDB 对连接公开的 close/shutdown 契约；共享连接上的读路径与线程内未提交事务的隔离程度尚需专项验证。不能从 WAL 三个字推断跨进程/断电强一致性。

[RECOMMENDATION] 后续应明确“DB 请求入队”“SQL 已提交”和“文件已交付”之间的可恢复断点。当前已经有独立 writer 队列，主线程的同步 insert/delete 与后台批量写仍共享 TaskDB 写锁。验收应覆盖 batch 回滚、线程 B 不入线程 A 事务、退出 drain 超时及重启恢复不复用 run_id；已有测试入口 [test_task_db_pagination.py](../../../../tests/test_task_db_pagination.py#L198)、[test_db_writer_batch.py](../../../../tests/test_db_writer_batch.py)，本轮未执行。

## 3. Cookie 的五级流转与写入闸门

```text
浏览器/WebView2/手动来源
  → AuthService 来源缓存及平台账户
  → CookieSentinel 校验后原子替换平台真相源
  → 本次请求冻结是否使用 YouTube Cookie
  → 托管真相源复制为 runfile → yt-dlp 读写 runfile → 已进入清理保护区的路径 finally 删除
```

[CONFIRMED] `_commit_to_truth_source(src, platform, source_tag)` 先解析 Netscape Cookie，再调用平台必需字段校验；失败保持目标文件不变并记录 commit warning；成功写 `.txt.tmp`、os.replace，再更新 meta 与状态。[CookieSentinel._commit_to_truth_source](../../../../src/fluentytdl/auth/cookie_sentinel.py#L382)

[CONFIRMED] `force_refresh_with_uac` 用 `_update_lock` 保护 `_updating` 平台集合，None 同时占两平台；busy 直接返回，finally 释放占用。刷新失败仍保留旧真相源。[CookieSentinel.force_refresh_with_uac](../../../../src/fluentytdl/auth/cookie_sentinel.py#L707)

[INFERRED] 先验后换解决“提取成功但只有半份凭据”覆盖仍有效凭据的问题。按平台锁避免 YouTube 刷新占住 X；同时要求平台参数正确，否则合法的另一平台凭据会被错误规则清掉。这是保留可恢复状态，不代表旧 Cookie 永远有效。

[CONFIRMED] `cookie_runfile()` 对两平台托管真相源做 realpath 匹配；mkstemp 后先 close(fd)，再原始字节 copyfile，with 退出时 remove。非托管路径和 None 原样直通。启动 sweep 仅按专用前缀与默认一小时年龄过滤。[cookie_runfile](../../../../src/fluentytdl/auth/cookie_runfile.py#L37)、[sweep_stale_cookie_runfiles](../../../../src/fluentytdl/auth/cookie_runfile.py#L91)

[CONFIRMED] 源码明确说明该隔离是因为 yt-dlp 结束时会回写 Cookie jar；字节复制避免引入第二次解析清洗；提前释放 Windows 文件句柄使子进程可重新打开和改写。真正运行入口在 [run_dump_single_json](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L1073)、[DownloadExecutor Cookie 生命周期](../../../../src/fluentytdl/download/executor.py#L432)、[workers.py runfile 包装](../../../../src/fluentytdl/download/workers.py#L2057)。

[CONFIRMED] 受管路径识别函数异常时返回 False，同样直通原路径。[识别边界](../../../../src/fluentytdl/auth/cookie_runfile.py#L37)。轻量提取在 [2052–2057 行](../../../../src/fluentytdl/download/workers.py#L2052) 创建 ExitStack 并取得 runfile，但显式清理 try 从 [2162 行](../../../../src/fluentytdl/download/workers.py#L2162) 才开始，close 位于 [2325 行](../../../../src/fluentytdl/download/workers.py#L2325)。[INFERRED] 中间 staging/env 初始化异常可能绕过该显式 close；对象回收与异常引用对实际残留时间的影响尚未运行验证，不能宣称已发生永久泄漏。

[UNKNOWN] 不能按 AGENTS 中“只读两个真相源”的字面语句宣称 CLI 总是直接读真相源：当前已有 runfile 隔离，也明确保留用户自管路径直通。平台 `.txt` 与 `.meta` 是两次写入，尚未证明断电后两者具有共同事务性；runfile 年龄 GC 未校验 owner，不能保证超过一小时的另一实例运行副本绝不被删除。

[RECOMMENDATION] 方案应分别定义“来源有效”“真相源已提交”“请求已附带凭据”“服务端接受凭据”，禁止把本地字段齐全等同于在线登录成功；进一步核查同平台手动导入、切账号、后台刷新是否都共享一个提交锁，以及固定 `.txt.tmp` 是否可能被并发提交争用。

## 4. 请求快照、缓存 generation 与独立开关

[CONFIRMED] `_snapshot_request` 用 ContextVar 保存 Cookie 模式与 generation，嵌套请求沿用外层 generation，finally reset；结果及 entries 被 stamp。`enforce_cookie_mode` 在 false 时移除三个 Cookie 参数键并清 SABR scope。[YoutubeService._snapshot_request](../../../../src/fluentytdl/youtube/youtube_service.py#L144)、[youtube_request.py](../../../../src/fluentytdl/utils/youtube_request.py#L5)

[CONFIRMED] Cookie 模式配置变化会增加 `_cookie_generation` 并清缓存；cache key 含 Cookie 模式及 generation；写缓存时持锁比对请求 generation 与当前值，过期请求拒绝回填。缓存使用 monotonic 时间、deepcopy、按 mode 的 LRU 上限；POT 预热状态不稳定和过大 entries 会跳过写入。[配置变化](../../../../src/fluentytdl/youtube/youtube_service.py#L235)、[cache key](../../../../src/fluentytdl/youtube/youtube_service.py#L1586)、[cache put](../../../../src/fluentytdl/youtube/youtube_service.py#L1697)

[INFERRED] 仅清空缓存不够：旧请求可能在清空后完成，把旧登录模式的结果写回；generation 是拒绝这次迟到写入的条件。deepcopy 防止 UI/选择逻辑修改共享缓存条目。队列 options 持久化快照则避免用户改开关后旧任务悄悄切换身份。

[CONFIRMED] 相关现有测试入口包括既有 worker 保留模式、匿名 SABR 不污染所选账户、解析期间切换拒绝旧缓存回填、POT 独立、任务模式持久化。[test_youtube_cookie_mode.py](../../../../tests/test_youtube_cookie_mode.py#L79)。[UNKNOWN] 本轮未执行，不代表所有重试/轻量提取/跨线程路径已通过运行验收。

## 5. 外部运行时、插件、端口与进程所有权

| 资源 | 现状与生命周期 | 为什么需要/保留的边界 |
|---|---|---|
| yt-dlp 二进制身份 | [CONFIRMED] resolve_runtime 优先有效 custom，再 local/PATH、frozen legacy；probe_version 读取选中二进制自己的 verbose 版本，缓存 key 为路径+size+mtime_ns+ctime_ns，带锁与超时。 | [INFERRED] 状态页与执行必须描述同一文件，不能把托管工具版本冒充 custom/PATH 版本。[ytdlp_runtime.py](../../../../src/fluentytdl/utils/ytdlp_runtime.py#L22) |
| FFmpeg / JS runtime | [CONFIRMED] prepare_yt_dlp_env 从干净环境起步，注入 UTF-8，优先配置工具位置，再定位运行时，Deno 优先，目录写入子进程 PATH。 | [INFERRED] 子进程依赖定位不应由用户全局 PATH 偶然决定；JS runtime 与 Cookie/POT 是不同能力。[prepare_yt_dlp_env](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L325) |
| POT 插件文件 | [CONFIRMED] _pot_sync_lock 串行部署，逐文件临时写+flush+fsync+字节验证+replace+再次验证，Windows 共享错误最多三次；最终逐项核对。 | [INFERRED] 防止半写文件、旧残缺插件被提前加载；这是逐文件原子，不是整个插件集合一次性事务。[sync_pot_plugins_to_ytdlp](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L242) |
| 插件搜索目录 | [CONFIRMED] 无法写 exe 邻接目录时可回退到打包只读源；命令插入 --no-plugin-dirs、验证目录、default；要求 POT 但无有效插件时抛错停止该请求。 | [INFERRED] 编译版 yt-dlp 不应仅依赖 Python 的 PYTHONPATH；先指定已验证目录避免坏旧副本抢先加载。[pot_plugin_directory](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L307)、[prepare_yt_dlp_env](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L374) |
| POT 服务进程 | [CONFIRMED] POTManager 持有 Popen、port、lock、warm Event/thread、重试时刻；atexit stop；start 在锁内启动 provider server --host 127.0.0.1，标准输出/错误 DEVNULL。 | [INFERRED] 每次请求不重复启动服务降低等待成本，但进程存活和能铸 Token 不是相同健康状态。[POTManager.__init__](../../../../src/fluentytdl/youtube/pot_manager.py#L46)、[start_server](../../../../src/fluentytdl/youtube/pot_manager.py#L181) |
| Windows Job | [CONFIRMED] 创建匿名 Job 并设置 KILL_ON_JOB_CLOSE，启动后尝试 AssignProcessToJobObject，失败降级记录警告。 | [CONFIRMED] 源码说明具名 Job 被实例共享时，别的句柄存活会让强杀后的服务成为孤儿；匿名 Job 才按应用实例拥有。[POTManager._setup_job_object](../../../../src/fluentytdl/youtube/pot_manager.py#L70) |
| 进程退出兜底 | [CONFIRMED] ProcessManager 保存 PID set、atexit/信号回调；先 callbacks，再已注册 PID；按名兜底还要求 ppid==自身，不是全局按名字杀。 | [INFERRED] 不应误杀用户另开的 ffmpeg/yt-dlp；PID set 本身未带创建时间，需考虑 PID 重用与并发注册边界。[ProcessManager.cleanup](../../../../src/fluentytdl/core/process_manager.py#L106)、[_cleanup_by_name](../../../../src/fluentytdl/core/process_manager.py#L194) |

[CONFIRMED] POT 本地 HTTP 明确绕过环境代理；手动代理注入 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，system 分支不主动注入。源码说明是防 TUN 双重代理。[POTManager._local_urlopen](../../../../src/fluentytdl/youtube/pot_manager.py#L139)、[代理配置](../../../../src/fluentytdl/youtube/pot_manager.py#L214)

[CONFIRMED] POT `start_server` 先调用 `_cleanup_orphan_servers`，向默认端口范围逐个 POST /shutdown，当前函数没有 PID/nonce/owner 验证；随后探测空闲端口并启动服务。[POTManager._cleanup_orphan_servers](../../../../src/fluentytdl/youtube/pot_manager.py#L151)

[INFERRED] 这与匿名 Job 的实例隔离目标存在潜在冲突：新实例可能关闭另一活跃实例的服务。探测后放开 socket 再启动存在端口抢占窗口。不能仅以“Job 已匿名”宣布多实例资源隔离完整。

[UNKNOWN] 未运行两个实例验证该冲突；未证明当前所有 Popen 均注册 ProcessManager。POT 的 OpenProcess 临时句柄未见显式 close，应核查 pywin32 对象释放与失败路径；Job handle 在 stop_server 中也没有显式关闭，可能刻意保留供重启，需明确最终 owner 生命周期。

[RECOMMENDATION] 后续验收至少区分监听端口、HTTP 健康、Token 实际生成、yt-dlp 插件实际发现四层，不以监听成功代替可下载。POT 现有 [try_recover](../../../../src/fluentytdl/youtube/pot_manager.py#L416) 会清 cache→重置 integrity token→重启，并验证生成能力，应保留这个分层。

## 6. 可观测性与隐私链

[CONFIRMED] emit_event 把字段构造为 JSON-safe Event，通过 loguru extra 写结构化记录；内部异常不向业务抛出。kind 位置参数允许业务字段也叫 kind，避免 Python 参数绑定在 try 之前抛错。[emit_event](../../../../src/fluentytdl/observability/events.py#L382)

[CONFIRMED] initialize_logging 安装全局 patch_record；patcher 对 message/extra 做脱敏，脱敏自身失败时替换消息而不是原样泄漏。sink 再次 redaction，诊断包导出再脱敏。[logger.initialize_logging](../../../../src/fluentytdl/utils/logger.py#L15)、[log_runtime.patch_record](../../../../src/fluentytdl/utils/log_runtime.py#L64)、[bundle.export_bug_bundle](../../../../src/fluentytdl/observability/bundle.py#L87)

[CONFIRMED] sanitize_path 折叠用户目录保留结构；sanitize_url 去 userinfo 并调用共享脱敏；sanitize_proxy 只留 scheme/host/port。应描述为明确规则覆盖，不是“所有敏感内容均不会出现”的证明。[sanitize.py](../../../../src/fluentytdl/observability/sanitize.py#L52)

[CONFIRMED] JSONL sink 有受锁保护的打开文件 LRU，上限 8，淘汰 close，单 JSONL 上限常量是 10 MiB（其旁注“512 MB”已过时）；trace enqueue=True，error 同步通道 enqueue=False；flush_sinks 等 logger.complete 并 flush 文件，要求从后台调用。[sinks._lru_get](../../../../src/fluentytdl/observability/sinks.py#L72)、[install_sinks/flush_sinks](../../../../src/fluentytdl/observability/sinks.py#L321)

[CONFIRMED] 原始 yt-dlp dump 写入时也脱敏，默认值得保存的终态仅 failed；日志维护有总量 200 MiB 预算，活跃文件登记用于清理。[write_raw_dump](../../../../src/fluentytdl/observability/sinks.py#L190)、[log_runtime.sweep_logs](../../../../src/fluentytdl/utils/log_runtime.py#L184)

[CONFIRMED] bug bundle 按 task/flow/run/session 选择，先 flush、固定 cutoff；预算不足写 missing/truncated；manifest 保存 privacy_version、源文件大小、实际导出 SHA256 与健康快照；ZIP 先写唯一 tmp。[export_bug_bundle](../../../../src/fluentytdl/observability/bundle.py#L87)

[INFERRED] 这些机制分别防止日志故障反过来打断业务、异步日志尚未落盘导致导出缺证、日志无限占盘、诊断包缺失被误判为“没有发生”。`signal` 与 `diagnosis`、task 与 run 必须保持语义边界，否则计数不再代表事实。

[UNKNOWN] 全部自由文本、外部工具输出的敏感信息模式是否充分覆盖，必须用伪造凭据样本验证；不能读取真实用户凭据来证明。日志持有句柄最终进程退出以外的关闭路线、不同 sink 初始化失败后的恢复一致性仍应加入生命周期章节。

## 7. 更新产物与回滚资源

[CONFIRMED] update_transport.download 增量计算 SHA256，只有参数非空时校验，失败删除目标。[Transport.download](../../../../src/fluentytdl/core/update_transport.py#L201)。[INFERRED] 方案不能笼统写“每个更新始终强制哈希校验”；应逐调用确认 manifest hash 必填政策。

[CONFIRMED] ComponentUpdateManager 有归档重复通知去重、generation 化待确认状态以及活跃任务检测；更新不等于下载一个归档后立即覆盖。[request_app_core_update](../../../../src/fluentytdl/core/component_update_manager.py#L427)

[CONFIRMED] updater 的 ready 模式要求 PID+nonce 双匹配；没有 READY 协议的旧主程序走 survival 宽限，避免错误超时杀掉健康新版。commit 清理旧 exe/_internal、归档、临时目录和 READY 文件；清理失败不改变更新已成功这一事实。[decide_watch_outcome](../../../../src/fluentytdl/core/updater.py#L1125)、[_commit_update](../../../../src/fluentytdl/core/updater.py#L1180)

[CONFIRMED] rollback 由 updater 恢复旧 exe/_internal、删除同批 updater.exe.new、以降权链启动旧版本，并避免给旧版传不认识的新 CLI 参数。源码说明回滚留在 updater 是因为它仍有替换 Program Files 内提权产物的权限。[updater._rollback_update](../../../../src/fluentytdl/core/updater.py#L1334)

[CONFIRMED] updater.exe.new 自更新先尝试原子替换；正在运行的 updater 需要延后 helper，失败保留 .new 供下次重试。该文件是升级 updater 自身所需的引导路径，不能当垃圾清理掉。[updater._self_update_updater](../../../../src/fluentytdl/core/updater.py#L1281)

[INFERRED] 更新的不可逆边界是新程序启动协议完成后的 commit；下载完成、解压完成、进程启动均不是同一个成功定义。应用更新、工具更新、卸载清理必须写成三个不同生命周期，卸载删除数据的规则不能复用到更新回滚。

## 8. 方案必须回答的剩余问题与验收边界

| 问题 | 当前证据强度 | 后续方案需明确的内容 |
|---|---|---|
| 配置保存失败是否可见 | [CONFIRMED] save 静默吞错 | [RECOMMENDATION] 可写性失败的用户反馈、原子配置保存、内存/磁盘一致性；本轮不改代码。 |
| DB writer 与关闭归属 | [CONFIRMED] 共享连接+写锁、独立 writer 线程、毒丸+join；[UNKNOWN] TaskDB 连接显式 close 和超时后排空结果 | [RECOMMENDATION] 区分发送停止请求、线程已退出、SQL 已提交、连接已关闭，核查超时异常契约。 |
| Cookie 真相源并发 | [CONFIRMED] 校验后 replace、平台刷新锁；[UNKNOWN] 所有提交来源共同互斥 | [RECOMMENDATION] 切账户/手动导入/后台刷新竞态，meta 与 txt 恢复策略，自管文件的明确边界。 |
| runfile 崩溃清理 | [CONFIRMED] 前缀+一小时年龄 | [RECOMMENDATION] 长任务与多实例的 owner 识别，不把“足够旧”当作确定未使用。 |
| POT 多实例与端口 | [CONFIRMED] 匿名 Job；无 owner 的端口范围 shutdown | [RECOMMENDATION] 端口与进程 owner/nonce 协议，冲突恢复、Job 失败降级的验收。 |
| 子进程与句柄 | [UNKNOWN] 所有外部进程是否纳管 | [RECOMMENDATION] 每一 spawn 标 owner、终止/kill/wait/close 与强杀后的 OS 回收范围。 |
| 更新完整性 | [CONFIRMED] hash 支持但底层可空；READY 有强/弱两模式 | [RECOMMENDATION] 各调用的校验要求、跨版本协议兼容矩阵、弱监护限制、故障注入点。 |
| 隐私和诊断 | [CONFIRMED] patch/sink/export 多层脱敏 | [RECOMMENDATION] 使用合成 Cookie/token/proxy/path 样本做泄漏回归，不读取真实凭据。 |

已有测试入口是后续验收资产，不是本轮通过证明：[Cookie 真相源](../../../../tests/test_cookie_truth_source.py)、[runfile](../../../../tests/test_cookie_runfile.py)、[Cookie 模式](../../../../tests/test_youtube_cookie_mode.py)、[POT 插件部署](../../../../tests/test_pot_plugin_sync.py)、[POT Job](../../../../tests/test_pot_job_object.py)、[POT 预热退避](../../../../tests/test_pot_warm_backoff.py)、[runtime identity](../../../../tests/test_ytdlp_runtime.py)、[数据迁移](../../../../tests/test_paths_migration.py)、[更新传输](../../../../tests/test_update_transport.py)、[updater](../../../../tests/test_updater.py)、[可观测性契约](../../../../tests/test_observability_contract.py)。
