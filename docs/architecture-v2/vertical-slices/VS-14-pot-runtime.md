# 新版架构 V2：VS-14 POT 服务、插件和外部运行时

2026-09-19 静态切片，未启动provider、yt-dlp、JS runtime或网络探测。`[CONFIRMED]`代码事实；`[INFERRED]`作用/风险；`[UNKNOWN]`待证；`[RECOMMENDATION]`后续。当前POT是独立Provider EXE提供本地HTTP服务，不能称为Node服务；Node仅可能出现在yt-dlp可选JS runtime配置中。

## 用户入口到实际外部组件

| 路径阶段 | 实际调用与结果 | 缘由与代价 |
|---|---|---|
| `[CONFIRMED]` 设置开关/启动 | `_on_pot_provider_toggled`保存设置并ensure_warm_async；main启动也调用预热 | `[INFERRED]` UI不等待网络token请求，首个解析任务共享预热进度。[开关](../../../src/fluentytdl/ui/settings_page.py#L2192)、[启动](../../../main.py#L498) |
| `[CONFIRMED]` 创建预热 | ensure_warm_async在Lock内检查已有线程和monotonic退避时刻，创建daemon pot-warm | `[INFERRED]` 防每个任务重复启动/重复铸token，代价是失败状态影响并发请求。[ensure](../../../src/fluentytdl/youtube/pot_manager.py#L823) |
| `[CONFIRMED]` 最低服务组件 | start_server定位provider EXE，清端口范围残留，选端口，Popen `[exe,server,--host,127.0.0.1,--port,...]`，Windows Job纳管 | `[INFERRED]` loopback服务与主应用解耦；端口与进程owner是两种资源。[start](../../../src/fluentytdl/youtube/pot_manager.py#L181) |
| `[CONFIRMED]` 能力确认 | verify_token_generation→插件/minter/deno状态记录；任务build_ydl_options调用wait_until_ready | `[INFERRED]` 活进程不等于能生成token，不等于插件已发现。仅状态卡定时读取内存status_brief。[warm](../../../src/fluentytdl/youtube/pot_manager.py#L841)、[任务等待](../../../src/fluentytdl/youtube/youtube_service.py#L564)、[UI轮询](../../../src/fluentytdl/ui/settings_page.py#L2237) |
| `[CONFIRMED]` 注入实际CLI | get_extractor_args返回youtubepot-bgutilhttp base_url；prepare_yt_dlp_env部署插件并指定搜索目录 | `[INFERRED]` 编译版CLI不能仅靠PYTHONPATH；修正插件位置才使服务被实际消费。[args](../../../src/fluentytdl/youtube/pot_manager.py#L792)、[env](../../../src/fluentytdl/youtube/yt_dlp_cli.py#L374) |
| `[CONFIRMED]` 一键检测/修复 | Settings→PotDiagnoseWorker后台→finished QueuedConnection→报告InfoBar、恢复按钮 | `[INFERRED]` 主动检测可能联网数十秒，不能放UI定时器。[入口/worker](../../../src/fluentytdl/ui/settings_page.py#L2261)、[结果](../../../src/fluentytdl/ui/settings_page.py#L2291) |

## 状态、资源与跨请求复用

[CONFIRMED] POTManager持 `_process`、`_active_port`、`_is_running`、`_job_handle`、`_lock`、`_warm_event`、`_warm_thread`、`_warm_attempts`、`_warm_retry_at`、token长度与minter数量。is_warm定义为至少成功生成一次token，不是当前所有能力实时都好。[字段](../../../src/fluentytdl/youtube/pot_manager.py#L46)、[is_warm](../../../src/fluentytdl/youtube/pot_manager.py#L803)

[CONFIRMED] wait_until_ready在任务线程最多join预热45秒并返回is_running&&is_warm；ensure_warm_async失败退避为30/120/300秒，检查时刻到了才允许再次触发，不是独立timer主动调度。[wait/ensure](../../../src/fluentytdl/youtube/pot_manager.py#L807)、[schedule](../../../src/fluentytdl/youtube/pot_manager.py#L912)

[INFERRED] warm记录是能力曾成功的缓存，可能与随后进程/端口变化产生时间差。`[UNKNOWN]` 多线程stop/start/wait下warm Event的一致性及是否出现假就绪，本轮未验证。

## 运行时身份与插件文件

[CONFIRMED] yt-dlp runtime选择自定义有效路径优先，再定位local/PATH、frozen legacy；版本probe读取该文件自身，缓存key含路径与size/mtime/ctime。托管安装位置与实际custom/PATH执行文件不应合并。[resolve/probe](../../../src/fluentytdl/utils/ytdlp_runtime.py#L22)

[CONFIRMED] prepare_yt_dlp_env从clean env开始，UTF-8，FFmpeg和Deno/configured JS路径加入子进程PATH；设置页另外有deno/node/bun/quickjs候选。[环境](../../../src/fluentytdl/youtube/yt_dlp_cli.py#L325)、[JS候选](../../../src/fluentytdl/ui/settings_page.py#L4403)。`[INFERRED]` POT服务、JS runtime、Cookie认证独立，某一项关闭/缺失不能简单归因另一项。

[CONFIRMED] 插件同步受进程内Lock保护，逐文件temporary→fsync→字节校验→replace→再校验；最多三次共享失败重试。写不了exe邻接目录可回退打包只读源；命令先no-plugin-dirs，再验证目录，再default；要求POT却无有效插件时请求失败。[sync](../../../src/fluentytdl/youtube/yt_dlp_cli.py#L242)、[目录回退](../../../src/fluentytdl/youtube/yt_dlp_cli.py#L307)

[INFERRED] 防半文件和旧插件抢先加载；代价是启动请求可能有磁盘I/O，且不是跨进程集合事务。default仍保留，不能宣称插件来源只限内置。

## 失败、恢复、取消与退出

| 事件 | 现有处理 | 限制与代价 |
|---|---|---|
| `[CONFIRMED]` 服务找不到/启动失败 | start_server False，warm记录server_down并设退避 | `[INFERRED]` 明确停在前置能力阶段，不等同媒体下载失败。[warm](../../../src/fluentytdl/youtube/pot_manager.py#L841) |
| `[CONFIRMED]` token失败 | warm记录token_fail；try_recover清cache→重置integrity token→重启，每步验证生成 | `[INFERRED]` 从最小破坏恢复升级到进程重启，保留诊断分层。[recover](../../../src/fluentytdl/youtube/pot_manager.py#L416) |
| `[CONFIRMED]` 插件/deno探测失败 | warm会写警告；实际CLI env有自身插件可用性闸门 | `[INFERRED]` “服务已warm”不等同整个工具链健康。[warm plugin](../../../src/fluentytdl/youtube/pot_manager.py#L854)、[CLI闸门](../../../src/fluentytdl/youtube/yt_dlp_cli.py#L387) |
| `[CONFIRMED]` HTTP代理 | loopback请求用空ProxyHandler；手动代理注入provider env；system不主动注入 | `[INFERRED]` 防本地调用绕行代理或TUN双重代理；不保证所有OS网络层行为。[local](../../../src/fluentytdl/youtube/pot_manager.py#L139) |
| `[CONFIRMED]` 关闭服务 | Lock内terminate→wait2秒→kill，清process/port；atexit注册stop | `[UNKNOWN]` kill后未见wait；应用硬杀依赖Job成功纳管。[stop](../../../src/fluentytdl/youtube/pot_manager.py#L330) |
| `[CONFIRMED]` Windows Job失败 | 建立/关联异常只warning继续 | `[INFERRED]` 退化可运行但硬退出子进程回收保证变弱。[Job](../../../src/fluentytdl/youtube/pot_manager.py#L70) |
| `[UNKNOWN]` 单任务取消期间共享warm | 本轮未找到wait_until_ready参数中的cancel事件 | `[INFERRED]` join等待与用户取消不是同一协议；取消一个任务也不应无条件杀共享服务 |

## 所有权风险与验收

[CONFIRMED] `_cleanup_orphan_servers`对默认端口范围全部POST/shutdown，未验证PID/nonce；另有每实例匿名Job。[清理](../../../src/fluentytdl/youtube/pot_manager.py#L151)、[匿名Job](../../../src/fluentytdl/youtube/pot_manager.py#L70)。`[INFERRED]` 新实例可能停止旧实例服务，端口探测后到spawn存在竞态；不是已证线上故障。

[RECOMMENDATION] 受控验证应独立观察provider存活、HTTP响应、实际token生成、插件发现、最终CLI消费；增加双实例启动、Job分配失败、readonly插件目录、插件半写、预热超时/取消、stop后重启。现有 [test_pot_job_object](../../../tests/test_pot_job_object.py)、[test_pot_plugin_sync](../../../tests/test_pot_plugin_sync.py)、[test_pot_warm_backoff](../../../tests/test_pot_warm_backoff.py) 未在本轮执行。
