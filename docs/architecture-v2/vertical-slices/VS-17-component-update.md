# 新版架构 V2 · VS-17 外部组件检查与安装

证据：2026-09-19 source/static；未下载组件、未启动 worker、未改 PATH、未测试。CONFIRMED 是源码行为；INFERRED 是作用或风险；UNKNOWN 未证；RECOMMENDATION 后续验收。

## 1. 触发、身份与目标

[CONFIRMED] 设置组件卡调用 `DependencyManager.install_component(component_key)`：[settings_page，L164](../../../src/fluentytdl/ui/settings_page.py#L164)。检查由 UpdateCheckerWorker(QThread) 获取实际本地版本与远端版本，结果保存 download_url、expected_sha256 等；无 URL 安装请求会做一次带集合去重的补检查：[check_update，L247](../../../src/fluentytdl/core/dependency_manager.py#L247)、[install_component，L330–358](../../../src/fluentytdl/core/dependency_manager.py#L330)。

[CONFIRMED] `resolve_exe()` 回答当前候选运行文件在哪里，yt-dlp 共用 RuntimeIdentity，其它工具可从 managed/PATH 选择。managed 主 EXE 存在即返回；只有 PATH 分支检查 extra_exes（如 FFmpeg 的 ffprobe）配套，不能由 bundled 返回推导组件全套可用。`get_exe_path()` 回答安装目标，frozen 为 app/bin/component，开发为 assets/bin/component。[dependency_manager，L169–245](../../../src/fluentytdl/core/dependency_manager.py#L169)。安装只用后者 L355，不覆盖用户自定义/PATH 目标。

[INFERRED] 这区分“工具已可用”和“托管工具已安装”；代价是更新 managed 后运行仍可能选 custom，不能向用户把两者版本混报。安装完成的 plugin sync 跟随实际执行的 yt-dlp resolver，仍需记录其选中身份。

## 2. 进程与消息协议

| 步骤 | [CONFIRMED] 代码及数据 | 状态/并发/退出 |
|---|---|---|
| 父进程建 worker | [DownloaderWorker.__init__，L853–899](../../../src/fluentytdl/core/dependency_manager.py#L853) QObject 拥有 QProcess 和 stall QTimer | 它不是 QThread；父 GUI 事件循环消费标准输出。 |
| 启动 | [start，L909–947](../../../src/fluentytdl/core/dependency_manager.py#L909) 开发启动 Python main.py --update-worker；frozen 启动自身 EXE --update-worker | JSON 经 stdin 写入后 closeWriteChannel，包含 key/url/managed target/version/channel/hash/extras/proxy。 |
| 特殊入口 | [main，L303–307](../../../main.py#L303) → [run_worker，L169](../../../src/fluentytdl/core/updater_worker.py#L169) | 在 QApplication 之前分支；独立子进程进行网络/解压/替换。 |
| 下载 | run_worker 创建 temp，Transport.download，finally 关闭 session | hash 非空才校验；不应写成所有组件总是强 hash。源码 [Transport.download，L201](../../../src/fluentytdl/core/update_transport.py#L201)。 |
| 安装 | [handle_zip，L129](../../../src/fluentytdl/core/updater_worker.py#L129) 选择指定 EXE 到 temp；[safe_install，L85](../../../src/fluentytdl/core/updater_worker.py#L85) 替换旧文件 | 多个 EXE 逐一安装，不是整个组件包全组原子事务；附属 EXE 找不到可跳过，主 EXE 缺失失败。 |
| 完成协议 | print_message progress/status/done/error；父 [_on_ready_read，L956](../../../src/fluentytdl/core/dependency_manager.py#L956) 解析 JSONL | `done` 不立即发成功，等待 QProcess exit 0 且没有 error；崩溃/非零退出发稳定错误码。 |
| 善后 | [_on_install_finished，L391–416](../../../src/fluentytdl/core/dependency_manager.py#L391) 更新版本身份、同步 POT 插件、emit、deleteLater | 插件后置失败记 warning，不反转已安装成功；实际工具可用性需另验。 |

## 3. 失败、超时与资源清理

[CONFIRMED] stall 超时为 300000ms，每条 progress 重置；`_on_stalled` 先发明确错误再 kill QProcess，后续 CrashExit 不覆盖首次错误：[常量，L61](../../../src/fluentytdl/core/dependency_manager.py#L61)、[L901–905](../../../src/fluentytdl/core/dependency_manager.py#L901)、[L949–998](../../../src/fluentytdl/core/dependency_manager.py#L949)。status 消息不重置该计时；下载结束后长时间解压/替换也可能耗尽预算。没有在此类看到用户主动 cancel 接口，不能把普通下载的暂停能力推广过来。

[CONFIRMED] `safe_install` 在目标存在时处理占用，改名为 `.exe.old`，最多三次改名重试；移动失败尝试恢复旧文件；清旧文件异常吞掉。ZIP 多成员失败时没有整个组回滚。worker 通用错误/hash 错误尽力删下载 temp 并返回 1：[updater_worker，L85–127](../../../src/fluentytdl/core/updater_worker.py#L85)、[run_worker，L251–274](../../../src/fluentytdl/core/updater_worker.py#L251)。

[CONFIRMED] `kill_locking_processes()` 在 psutil 可用时匹配完整目标路径/打开文件；**psutil 导入失败分支使用 `taskkill /F /IM <name>` 全局同名强停**：[L52–82](../../../src/fluentytdl/core/updater_worker.py#L52)。这与一般 ProcessManager 的 owner 限制不同。[INFERRED] 非标准打包/依赖损坏时会扩大影响范围，不能声称全仓所有终止操作都按本实例 PID 树限定。

[CONFIRMED] yt-dlp 安装后写 manifest 的版本/channel，并清理目标目录其它内容，仅保留 executable、manifest.json 和 yt-dlp-plugins；目录归属依赖 managed target 正确：[run_worker，L226–249](../../../src/fluentytdl/core/updater_worker.py#L226)。[INFERRED] 这是工具目录维护，不应用于自管目录，调用端 get_exe_path 是重要保护边界。

## 4. 处理缘由与接受限制

[INFERRED] 组件子进程隔离阻塞网络和替换错误；stdin JSON 避免复杂命令行转义；成功等待进程退出避免文件仍锁定；stall watchdog 防网络半死无终结。代价是父子消息、退出码、目标安装文件、后续版本探测要共同解释，任何一个“done”都不足以证明完整工具组合健康。

[UNKNOWN] 真实路径下安装权限、psutil 缺失分支、ZIP 多成员中途失败、长安装被 watchdog kill、无 hash 上游策略均未动态验证。[RECOMMENDATION] 使用假组件/临时目录验证成功与 hash 错误；模拟主 EXE/附属 EXE 缺失和第二成员失败；记录 temp/.old 残留；验证 custom/PATH 字节未变及 UI 显示实际 runtime。已有 [test_dependency_manager](../../../tests/test_dependency_manager.py)、[test_update_transport](../../../tests/test_update_transport.py) 仅作为后续入口，本轮未运行。
