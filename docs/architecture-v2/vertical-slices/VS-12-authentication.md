# 新版架构 V2：VS-12 认证来源、账户与 WebView2 登录

静态调查日期2026-09-19。未登录、读取凭据或执行浏览器。`[CONFIRMED]` 为代码事实，`[INFERRED]` 为作用/风险推导，`[UNKNOWN]` 为待证行为，`[RECOMMENDATION]` 为后续建议。此切片终点是本地Cookie提交和UI反馈，不是视频可下载证明。

## 用户到最低层再返回

| 顺序 | 入口/组件/最低操作 | 返回与持久化 | 缘由及代价 |
|---|---|---|---|
| 1 | `[CONFIRMED]` 平台认证卡loginClicked(platform)、accountChanged(platform,id)、refreshCookieClicked(platform)→SettingsPage槽 | 根据操作禁用按钮、显示进行中/提示 | `[INFERRED]` UI只发平台事件，避免YouTube/X混用；实际槽仍调用服务。[PlatformAuthExpandCard](../../../src/fluentytdl/ui/components/settings/platform_auth_card.py#L15)、[Settings接线](../../../src/fluentytdl/ui/settings_page.py#L1722) |
| 2 | `[CONFIRMED]` `_on_webview2_login_clicked`预检运行时，设置来源WEBVIEW2；设置页有跨平台_webview2_login_in_progress互斥 | `_do_cookie_refresh(platform)`返回QThread；finished QueuedConnection恢复按钮、InfoBar | `[INFERRED]` 同时启动两个WebView窗口可能竞争，故UI先挡；这不是整个进程全入口的共同锁。[登录槽](../../../src/fluentytdl/ui/settings_page.py#L3628)、[UI互斥](../../../src/fluentytdl/ui/settings_page.py#L3672) |
| 3 | `[CONFIRMED]` CookieRefreshWorker.run→Sentinel.force_refresh_with_uac | bool/message/False信号；Sentinel按平台占用，finally释放 | `[INFERRED]` DPAPI与同步子进程等待不能放Qt主线程；need_admin当前固定False。[worker](../../../src/fluentytdl/ui/components/common/cookie_refresh_worker.py#L32)、[刷新锁](../../../src/fluentytdl/auth/cookie_sentinel.py#L707) |
| 4 | `[CONFIRMED]` Sentinel._update_from_browser→AuthService.get_cookie_file_for_ytdlp(platform,force_refresh) | WebView2仅force_refresh启动交互，否则用账户缓存；浏览器走rookiepy；FILE解析/清洗到来源缓存 | `[INFERRED]` 启动和普通下载不应自动弹登录窗口；代价是旧缓存需通过健康/失败反馈暴露。[来源路由](../../../src/fluentytdl/auth/auth_service.py#L483)、[Sentinel调用](../../../src/fluentytdl/auth/cookie_sentinel.py#L934) |
| 5 | `[CONFIRMED]` WebView2CookieProvider再次预检runtime，创建multiprocessing.Queue、Process(target=_webview_subprocess) | 子进程pywebview读取Cookie、Queue回传dict；父轮询队列兼查进程存活 | `[INFERRED]` 隔离Qt和pywebview事件循环；进程早死不再等完整330秒。[provider](../../../src/fluentytdl/auth/providers/webview2_provider.py#L507)、[wait](../../../src/fluentytdl/auth/providers/webview2_provider.py#L636) |
| 6 | `[CONFIRMED]` 子进程Cookie格式化→AuthService CookieCleaner→Netscape来源缓存；Sentinel解析+平台必需字段校验→tmp/os.replace | 更新平台真相源、meta、AuthStatus、账户状态；UI成功/失败 | `[INFERRED]` “提取到一些Cookie”不足以覆盖有效旧jar。来源缓存可以失败，真相源需另过闸门。[写缓存](../../../src/fluentytdl/auth/auth_service.py#L599)、[commit](../../../src/fluentytdl/auth/cookie_sentinel.py#L382) |

## 状态与数据

[CONFIRMED] 来源是AuthSourceType，平台当前账户存于 `_current_webview2_account_ids`；账户含account_id、platform、profile_dir、cached_cookie_path、valid、cookie_count、last_extracted_at、sabr_only。accounts.json version=1；主认证配置与账户索引不在同一路径。[模型](../../../src/fluentytdl/auth/auth_service.py#L192)、[初始化路径](../../../src/fluentytdl/auth/auth_service.py#L310)、[保存账户](../../../src/fluentytdl/auth/auth_service.py#L1363)

[CONFIRMED] Settings账号切换调用`set_current_webview2_account`，该函数先更新并保存当前ID，尝试同步真相源，但忽略同步bool并返回True；无缓存/校验失败会保留旧真相源，UI仍可提示已切账号。[切换](../../../src/fluentytdl/auth/auth_service.py#L1307)、[同步失败](../../../src/fluentytdl/auth/auth_service.py#L1319)、[UI反馈](../../../src/fluentytdl/ui/settings_page.py#L3758)

[INFERRED] “选择账户成功”和“该账户Cookie成功激活”是两个不同结果。保留旧Cookie防止不可用新账户破坏现有下载，代价是选择账户与有效凭据来源可能不一致。`[RECOMMENDATION]` 正式状态设计需同时表达selected account、effective source与commit warning，不用单一登录成功标签覆盖。

## 并发、失败、重试、取消与退出

| 场景 | 当前事实 | 限制/处理缘由 |
|---|---|---|
| 正常登录 | `[CONFIRMED]` 子端put→queue.close→join_thread，随后延迟销毁窗口；父取cookies后返回 | `[INFERRED]` 先刷管道防窗口关闭过快丢结果。[send](../../../src/fluentytdl/auth/providers/webview2_provider.py#L122) |
| 缺runtime/CLR/pywebview | `[CONFIRMED]` runtime服务层预检；CLR返回code/stage，pywebview import失败也发error | `[INFERRED]` 区分缺WebView2与Python.NET加载失败，避免错误引导安装错误组件。[preflight](../../../src/fluentytdl/auth/providers/webview2_provider.py#L518)、[CLR](../../../src/fluentytdl/auth/providers/webview2_provider.py#L169) |
| 子进程早退 | `[CONFIRMED]` 每1秒poll，确认死亡后再取队列0.5秒，仍无结果才subprocess_exited | `[INFERRED]` 防已成功发结果但进程先退被误判失败。[wait](../../../src/fluentytdl/auth/providers/webview2_provider.py#L651) |
| 用户关窗口 | `[CONFIRMED]` closed回调向Queue发用户关闭error | `[UNKNOWN]` 所有父窗口关闭/应用退出是否协调登录worker，本轮未证。[on_closed](../../../src/fluentytdl/auth/providers/webview2_provider.py#L404) |
| 超时/异常收尾 | `[CONFIRMED]` 父finally对存活process terminate、join5秒、仍存活kill | `[UNKNOWN]` kill后无再次join，父Queue/Process显式close未见；不宣称全句柄确定释放。[finally](../../../src/fluentytdl/auth/providers/webview2_provider.py#L627) |
| 同平台刷新竞争 | `[CONFIRMED]` Sentinel busy直接失败，不排等待队列，finally移除平台占用 | `[INFERRED]` 避免两份提取互相覆盖；切账号直调commit不一定共享该锁。[refresh](../../../src/fluentytdl/auth/cookie_sentinel.py#L720) |
| 浏览器App-Bound失败 | `[CONFIRMED]` 明确decrypt失败返回替代来源指引；权限类可PermissionError；UI有restart_as_admin | `[INFERRED]` UAC不能保证绕过所有加密限制。函数名with_uac不是实际提权证明。[提取](../../../src/fluentytdl/auth/auth_service.py#L841)、[UI权限](../../../src/fluentytdl/ui/settings_page.py#L3861) |
| 重试/重启 | `[CONFIRMED]` 用户再次刷新可强制提取；已有WebView2 profile跨启动保留；浏览器来源5分钟缓存可复用 | `[INFERRED]` profile保留改善登录体验，须按凭据资料而非普通缓存管理。[缓存](../../../src/fluentytdl/auth/auth_service.py#L811) |
| 删除账户 | `[CONFIRMED]` 每平台至少留1个；remove_storage可递归删除该账户根，ignore_errors=True；默认False | `[UNKNOWN]` 返回True不证明目录清完，UI选择与存储删除要分别验证。[delete](../../../src/fluentytdl/auth/auth_service.py#L1258) |

## 安全与观测

[CONFIRMED] 主日志只记录Cookie数量等，但子进程另有profile/webview_subprocess.log直接append，手动proxy_full可原样写入，绕过公共redactor。[子进程日志](../../../src/fluentytdl/auth/providers/webview2_provider.py#L109)、[proxy分支](../../../src/fluentytdl/auth/providers/webview2_provider.py#L155)。`[INFERRED]` 有凭据的代理URL可能落入旁路日志；`[UNKNOWN]` 未查真实数据。

[RECOMMENDATION] 用合成凭据/模拟子进程验证：登录成功与close竞争、runtime缺失、Queue读取失败、切到无缓存账户、平台锁竞争、UI关闭时后台仍活跃、明文日志旁路。已有Cookie真相源测试可复用，但本轮未执行，不等于该切片已验收。
