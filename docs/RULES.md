# FluentYTDL 开发规则

> [English version](RULES_EN.md)

## 1. 项目身份

- **名称**：FluentYTDL — 专业 YouTube/视频下载器
- **语言**：Python 3.12.12
- **UI 框架**：PySide6 (Qt6) + QFluentWidgets（Fluent 设计）
- **下载引擎**：yt-dlp CLI 子进程（非 Python API）
- **媒体处理**：FFmpeg
- **代码库**：148 个 .py 文件，~50k LOC，`src/fluentytdl/` 包
- **平台**：Windows 为主，跨平台为目标

## 2. 架构规则

### 分层架构

```
UI 层 (ui/)
  ↓ 依赖
服务层 (auth/, youtube/, download/, processing/, storage/)
  ↓ 依赖
核心基础设施 (core/)
  ↓ 依赖
基础层 (utils/, models/)
```

- **UI 绝不能直接调用 yt-dlp** — 通过 `youtube_service`
- **服务层绝不能从 ui/ 导入** — 通过 Qt Signal 通信
- **Models 自包含** — 无循环依赖

### 单例

项目广泛使用单例模式。关键单例：`config_manager`、`download_manager`、`auth_service`、`cookie_sentinel`、`youtube_service`、`pot_manager`、`task_db`。

创建新单例时，需在此列表中记录。

### Qt Signal/Slot

所有 UI-后端通信必须使用 Qt Signal/Slot 机制。绝不能在 UI 事件处理器中直接调用后端方法 — 发射信号代替。

### 六种解析模式

项目支持 6 种不同的解析模式。详见 `docs/ARCHITECTURE.md` 第 3 章：

1. **视频** — 标准单视频下载
2. **VR** — VR 视频，使用 `android_vr` 客户端，EAC 转换
3. **频道** — 频道标签页列表，懒加载
4. **播放列表** — 播放列表，批量操作
5. **字幕** — 独立字幕下载（轻量提取）
6. **封面** — 独立封面下载（直链或轻量提取）

修改下载逻辑时，必须考虑对所有 6 种模式的影响。

## 3. 代码风格

### Ruff（强制）

```toml
target-version = "py312"
line-length = 100
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]  # 允许长行
```

- `__init__.py` 文件中忽略 `F401`（重导出是有意的）
- isort：`known-first-party = ["fluentytdl"]`

### Pyright（建议性）

```toml
pythonVersion = "3.12"
# 很多 report* 设置已放宽 — 不要随意添加新的 type:ignore
```

### UI 规则 (PySide6-Fluent-Widgets 最佳实践)

- **必须**使用 QFluentWidgets (`FluentWindow`、`InfoBar`、`MessageBox` 等)。
- **绝不**使用原始 `QMessageBox`、`QDialog` 或纯 `QWidget` 来创建新 UI 组件，优先使用库内等效组件。
- **导入规范**：优先直接从 `qfluentwidgets` 导入组件（例如 `from qfluentwidgets import PushButton`），除非是纯布局类（如 `QVBoxLayout`），否则避免混合使用原生 `PySide6.QtWidgets`。
- **主题适配**：绝对禁止对颜色硬编码。必须使用 `isDarkTheme()` 配合或使用 `ThemeColor` 宏，以保证浅色/暗色模式的无缝切换。
- **路由与页面**：复杂的子界面必须继承自核心页面组件（如 `ScrollArea` 或适当的 `QWidget`），并通过主界面的侧边栏或路由器进行注册。
- **列表项优化**：列表项必须使用 `QPainter` 委托（避免大量列表带来巨大的 `QWidget` 性能开销）。
- **暗色模式支持**：用 `ui/components/common/custom_info_bar.py` 里的 `InfoBar`，而非直接从 `qfluentwidgets` 导入的那个。它是 `qfluentwidgets.InfoBar` 的子类（同名，按 `from fluentytdl.ui.components.common.custom_info_bar import InfoBar` 导入），补了暗色模式下的对比度处理。**没有**叫 `CustomInfoBar` 的类 —— 别照着这个名字去搜。
- **文字排版与字重**：严禁使用普通的 `BodyLabel` 作为标题或指令提示（会导致字体发虚）。必须使用 `StrongBodyLabel` 或 `SubtitleLabel`。
- **文字颜色对比度**：严禁硬编码使用 `Qt.GlobalColor.darkGray` 或 `QColor(160, 160, 160)`。次要文本（如 `CaptionLabel`）必须手动注入高对比度颜色：`setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))`，确保深色模式下清晰锐利。
- **SettingCard 安全修改**：严禁使用 Monkey Patch 全局修改 `SettingCard` 行为。自定义组件（如 `InlineComboBoxCard`）可能将 `contentLabel` 替换为无 `setTextColor` 方法的普通 `QLabel`，全局 Patch 会导致 `AttributeError`。应在页面级 `__init__` 中使用 `findChildren` 遍历，并结合 `hasattr` 进行安全处理。

### 文件命名

- 所有 Python 文件使用 snake_case
- 建议每个文件一个类（尤其在 ui/components/ 中）
- 私有模块级函数用 `_` 前缀

## 4. yt-dlp 集成规则 [关键]

这些规则来自生产环境的惨痛教训。违反它们**必然**导致用户可见的 bug。

1. **优先使用 yt-dlp 默认的 `player_client` 策略**（由实际内核版本及请求认证状态决定）——绝不*锁定*单一客户端。**例外（SABR-only 账号）：** 当 yt-dlp 报告账号命中 SABR-only 实验（`forcing SABR streaming` / `formats ... missing a url` → 高清格式没有直链、被丢弃，只剩 360p）时，向客户端集合**追加** `web_safari`（`default,web_safari`，或已设 POT 时的 `<现有>,web_safari`，例如 `default,mweb`）。这是*追加*而非锁定——默认客户端仍先跑，web_safari 只是找回直链高清（HLS）的兜底。在解析期侦测（`yt_dlp_cli._maybe_mark_sabr_only`），持久化**在账号上**（`WebView2Account.sabr_only`，登出时用内存会话标志），并在 `build_ydl_options` 中消费（`_maybe_append_sabr_web_safari`），从而同时覆盖解析**与**下载两条 opts 路径（各自独立构建 opts）。此为临时方案——随着采样更多视频可能需要调整。
2. **绝不启用 `sleep_interval`** — 导致签名 URL 过期 → HTTP 403
3. **绝不使用 `--cookies-from-browser`** — Windows 上导致 DPAPI 文件锁
4. **`-S lang:xx` 是失效的 —— 绝不用它表达语言偏好。** `lang` 是 `language_preference` 的**数值**别名，不接受语言码（喂语言码会把全局 `settings['lang']['convert']` 改成 `'string'`、拿 10/5/−1/−10 跟 `"ja"` 比），而且 `FormatSorter.add_item` 只接受**第一个** `lang:` 条目。语言与原音偏好必须走 `_inject_language_into_format()` 的格式串过滤器：`[language^=xx]`（startswith —— 裸 `[language=en]` 匹配不到真实标注 `en-US`）与原音的 `[language_preference>=?10]`（**`?` 是必须的，且必须紧跟运算符** —— 只有 YouTube 有 `language_preference`，不带 none-inclusive 会把 Twitter 等平台的所有音轨过滤光；而 yt-dlp 的过滤器语法把该标记放在运算符与值之间，写成值侧的 `>=10?` 会直接 `SyntaxError: Invalid filter specification`，整个下载起不来）。原始格式串永远作为最后兜底
5. **非零退出时验证文件大小** — Windows `.part-Frag` 删除失败但下载已完成
6. **同步 POT 插件到 exe 目录** — 编译后的 yt-dlp 无法通过 PYTHONPATH 发现插件
7. **TUN 模式不注入代理环境变量** — 注入 `HTTPS_PROXY` 导致双重代理
8. **web_music 需要 `disable_innertube=True`** — 该客户端的 InnerTube 挑战有缺陷
9. **BCP-47 别名扩展** — `zh-Hans` 必须匹配 `zh-CN`、`zh-SG` 等；音频与**字幕**都走 `utils/bcp47.py` 这一份。`--sub-langs` 的每一项被 yt-dlp 当作**锚定正则**去匹配真实字幕键，裸 `en` 匹配不到 `en-GB` —— 用户偏好必须先解析成真实键（或回落成 `en(-.+)?`）才能进命令行
10. **事务沙盒模型** — `download/staging.py` 的 `StagingArea` 全权管理产物生命周期。完整顺序：`create(事务级 uuid4 目录)` → `prepare_attempt(n)` → yt-dlp 以 `paths.home = payload/`、`paths.temp = .parts/` 运行 → 每行输出 `add_reported()` → `reconcile()`（唯一物理扫描，只扫 `payload/`）→ `seal_discovery()` → Feature 经 `StagingArea` 原子 API 变更 → `verify()`（安全门，二值，不做集合减法）→ `build_plan()` → `commit()`（取消门 → phase=committing → 预留整组 → WAL 逐项发布 → phase=committed）→ 不可逆点 → `finalize_failure()` 作为唯一失败/取消裁决点。四条绝对不可违反的约束：
    - **`outtmpl` 必须是相对路径且必须通过 `assert_inside`** —— 绝对 `-o` 会让 `-P` 完全失效，破坏类型化路径沙盒重定向。
    - **从 `reserve` 之前起，事务不可取消、不可被外部删除** —— `commit()` 内含唯一的取消门；触发后取消仅记 `pending_cancel`。`core/controller.py` 不得对活事务 `rmtree` 沙盒（会把正在提交的源文件从提交中抽走，违反硬约束 8）。
    - **`phase=committed` 是不可逆的成功边界** —— 提交后的异常（`emit_actual`、`cleanup`）只能成为 `kind=signal`；不得将 outcome 改为 `failed`。
    - **`delivered:*` token 不得被任何提交前的门消费** —— 它们在 `commit()` 之后才存在，`verify()` 中引用它们会让每个正常下载都被自己的安全门拦下。

详见 `docs/YTDLP_KNOWLEDGE.md` 完整经验知识库。

## 5. 观测事件层 [关键]

`src/fluentytdl/observability/` 是项目唯一的结构化日志层。统一走
`emit_event(kind, *, trace=None, level="INFO", stage=None, **fields)` 或 `trace.emit(...)` ——
`kind` 是 positional-only 参数，所以业务字段自己也可以叫 `kind`。

这一层的价值全靠下面五条守住。破了任何一条，`count(kind=...)` 就不再有意义 ——
而那正是它取代原先七条各说各话的观测通道的全部理由。

### 五条硬规则

1. **`transition` 只有一个权威产生点** —— `download_manager._on_unified_status()`，即"UI 状态变成持久状态"的那个边界。一次状态变化只允许一条 `transition`。多一个产生点就让计数翻倍，所有基于事件的统计一起失效。
2. **`diagnosis` 只属于失败错误边界。** 成功路径的异常征兆只能是 `signal`，绝不是 `diagnosis`；成功路径**绝不调用 `diagnose()`**（那是主因仲裁）。`signal` = 我观察到了什么；`diagnosis` = 这次失败最终判定为什么。只有这样分，`count(kind=diagnosis)` 才恒等于"真正发生了错误"。而且只有**构造出 `Diagnosis` 的那个 `except` 块**负责 emit，UI 层只消费、永不 emit。
3. **`degraded` 只能由 `expected − actual` 得出** —— 绝不是 `系统支持的 − actual`。用户没勾封面而封面不存在，不是降级。
4. **每个 `run_id` 恰好一个 `outcome`，且 Outcome 属于 run 而非 task。** 子系统只报告事实（`kind=recovery` / `kind=signal`），不得宣布最终结果；只有 run 的终态边界有权 emit `outcome`。`task=42 run=A→paused, run=B→success` 完全合理。
5. **观测永远 best-effort。** `emit_event()` 内部吞掉自己的一切异常，绝不上抛给业务层 —— 会抛异常的日志层，恰好会在它本该记录的那个错误现场自己炸掉。

### 五级标识

`session → flow → task → run → attempt`。规则驱动的自动重试调 `trace.next_attempt()`，
**`run_id` 保持不变**；只有软件重启后恢复同一任务才用 `trace.new_run()` 铸新 run。
若重试也换 run，`run_id` 和 `attempt` 就在表达同一件事，层次随即失去意义。

`flow_id` 存在的理由是 `parse` / `select` 发生在 `task_id` 出现之前 —— 四个解析 worker
全部跑在 `DownloadWorker` 之前。没有它，时间线里"下载之前"的那半截全是孤儿。
`create_worker()` 拿到 `db_id` 后发一条 `kind=identity`，把 flow → task 钉起来。

### EventKind 是封闭集合

```
stage  decision  expect  actual  signal  diagnosis  recovery
retry  transition  outcome  config  argv  identity
```

不许自行新增。刻意不设的两个：**`mismatch`**（用
`actual matched=false expected=... actual=...` 表达）和 **`ui_progress`**（进度刷新维持
现有 Qt Signal 通道 —— 把它写成事件会让 JSONL trace 变成进度数据库；只有语义阶段切换
才配得上 `kind=stage`）。

- `stage` ∈ `startup parse select preflight download postprocess verify finalize retry cancel`
- `outcome` ∈ `success failed cancelled paused interrupted restored_pending`；`recovered` 与 `degraded` 是正交标记，可以在同一条 outcome 上同时为真

### 字段纪律

- **只记 `code` / `category` / `severity`，绝不记本地化文案。** `Diagnosis.user_title` / `user_message` / `recovery_hint` 以及任何 `self.tr()` 串都会让日志内容随界面语言变化，破坏可搜索性。
- **落地前先脱敏** —— `sanitize_argv()` / `sanitize_url()` / `sanitize_path()` / `sanitize_exception()`。argv 里可能有 `socks5://user:pass@host`、cookie 路径、完整输出路径。绝不记 Token 明文，只记长度或 `base_url`。
- **`Event.as_dict()` 必须始终 JSON-safe**（`Path`→str、`Enum`→value、`set`→排序后的 list、未知对象→截断的 `repr`）。文本 sink 打得出来而 JSONL sink 抛 `TypeError`，就是硬规则 5 在最糟的时刻失效。
- **`utils/` 不得 import `observability`** —— `utils/logger.py` 会造成循环导入。Foundation 层模块接一个不透明的 `trace: Any = None` 并调 `trace.emit(...)`；`processing/` 与 `youtube/` 属于 Service 层，直接 import 即可。
- **只在真的变了时才 emit。** "检查过、保持原样"不是决策 —— 记下来会让 `count(kind=decision subsystem=container)` 不再等于"容器被系统改过几次"。
- **绝不改动任何 loguru sink 的 `format` 字符串。** `emit_event()` 自己把 `key=value` 渲染进 message，机器可读的 dict 走 `logger.bind(fytdl=...)` 进 `extra`，只给 JSONL sink 消费。这正是 `log_viewer_window` 与 `log_signal_handler` 零风险的原因。

五条硬规则各有一条对应断言在 `tests/test_observability_contract.py` 里。**规则不配测试等于没有规则。**

## 6. Cookie 系统规则 [关键]

### YouTube 请求级 Cookie 模式

- `youtube_cookies_enabled` 默认开启，仅控制请求是否携带 Cookie；账号、Cookie 文件和同步保留。
- 解析开始固定 `__fluentytdl_youtube_cookies_enabled`，随队列任务持久化；切换设置不改变已有任务。匿名模式覆盖直接文件、Sentinel、参数合并、轻量提取和重试路径。
- 缓存代次阻止切换前请求回写；SABR 根据实际请求 Cookie 上下文隔离，不能仅根据选中的账号打标。
- POT、JS 运行时保持独立；匿名使用上游默认客户端，本功能不追加登录态 visionos，也不排除 web。
- `utils/ytdlp_runtime.py` 统一实际路径和版本；组件安装目标独立，更新不能覆盖自定义或 PATH 内核。

### 两个真相源

`bin/cookies_youtube.txt` 与 `bin/cookies_twitter.txt` 是 yt-dlp **唯一**会读的 Cookie 文件。`yt_dlp_cli.py` 里注入 `--cookies` 的那一处是唯一漏斗 —— 不要再加第二条 Cookie 路径，**绝对不要**用 `--cookies-from-browser`（§4.3，DPAPI 文件锁）。`bin/dle_user/<platform>/` 下的账号缓存、浏览器提取结果，都只是上游素材，必须**提交**进真相源才会生效。

路径一律走 `cookie_sentinel.get_cookie_path_for_platform(platform)` / `get_meta_path_for_platform(platform)`。代码里不再有任何东西绑定裸 `cookies.txt`；带 `platform` 形参的方法不得悄悄用默认值兜底 —— 传错平台是唯一会毁掉**另一个**平台凭证的故障模式。

### 写入闸门（弱回退）

`CookieSentinel._commit_to_truth_source(src, platform, source_tag) -> (ok, reason)` 是写真相源的**唯一**入口。它解析候选文件、跑 `auth_service._validate_cookies(cookies, platform)`，只有通过才原子替换目的地（`.txt.tmp` → `os.replace`）并写 `.txt.meta` 侧写文件。

- **弱回退**：校验不过则旧文件**字节级不变**。"提取成功但内容是半份 / 全部过期"绝不允许覆盖一个还能用的文件。不要再往真相源上写裸 `shutil.copy2`。
- 拒绝原因按平台留存（`get_commit_warning(platform)`），并通过 `get_status_info(platform)["commit_warning"]` 送到 UI，让界面能说"仍在使用较旧的文件"而不是静默失败。
- **必需 Cookie 分平台**：YouTube `SID / HSID / SSID / SAPISID / APISID`；X `auth_token / ct0`。会话 Cookie（`expires == 0`）不算过期。
- **懒清理**：新提取成功提交之前，绝不删除旧 Cookie。

### 刷新并发与线程

`force_refresh_with_uac(platform=None)` 持**按平台**的互斥锁（`self._updating` 集合；`None` 同时占用两个平台）。刷新 YouTube 不得挡住刷新 X。

刷新会读 DPAPI 数据库、会等 WebView2 子进程，所以**绝不能**跑在 Qt 主线程 —— 那正是用户报的卡死。所有 UI 入口统一走 `CookieRefreshWorker(QThread)`，并用 `Qt.ConnectionType.QueuedConnection` 连接它的 `finished` 信号。`CookieSentinel` 是 `QObject`，通过 `startupHealthReady = Signal(dict)` 上报启动状态；它不得 import `ui/`。

### 启动行为

启动刷新是**静默**的。`get_startup_health()` 按平台返回 `{enabled, exists, valid, expiring_soon, expiry_seconds, reason, commit_warning}`，UI 只对 `enabled=True` 的平台提醒 —— 只下载 X 的用户绝不该被 YouTube 的状态骚扰。有效则完全不出声；即将过期 → `InfoBar.info`；缺失或失效 → `InfoBar.warning`。不要再靠定时器猜时序去弹窗。

### 提取源

- **`auth_service.py` 的 `BROWSER_COMBO_ITEMS` 是浏览器下拉框顺序的唯一来源**，`BROWSER_SOURCES` 与 `BROWSER_COMBO_LABELS` 由它派生，索引映射用 `browser_source_at()` / `browser_combo_index()`。UI 层不要再手写位置列表。
- **不再支持 Chrome 与百分浏览器（CentBrowser）**。枚举成员故意保留，避免 `AuthSourceType(旧值)` 抛异常；`_load_config()` 会把它们一次性迁到 `EDGE` 并写回配置。
- **WebView2 模式必须先过 `is_webview2_runtime_available()`**（`auth/webview2_runtime.py`，查注册表）。缺少运行时要弹 `MessageBox` 给出"下载 / 改用浏览器提取 / 手动导入"三个出口 —— 而不是让用户干等五分钟队列超时。子进程里 `webview.start()` 必须包 `try/except`，父进程等待时轮询 `process.is_alive()`。
- **Chromium v130+**：需要管理员权限进行应用绑定加密解密（`ADMIN_REQUIRED_BROWSERS`）。
- **403 错误恢复**：自动检测 Cookie 过期关键字，只刷新出问题的那个平台。
- **JSON Cookie 文件**：拒绝并警告（yt-dlp 仅支持 Netscape 格式）。
- **WebView2 模式**：WebView2 是项目内基于 WebView2 的 Cookie 提取机制的新名称（以前称为 DLE）。在代码中，必须始终使用 `AuthSourceType.WEBVIEW2` 和术语 `webview2`。在 UI 文本中，使用 `登录获取 (WebView2)` 或 `WebView2 登录`。**绝对不要**将其重新命名回 `DLE`。
- **WebView2 提取器**：当前实现使用 `WebView2CookieProvider`。在代码中引用时使用 `WebView2CookieProvider`，在用户界面中使用 `WebView2`。

### 合规清洗

`CookieCleaner.clean(cookies, platform, enable_cleaning, *, drop_expired=True)`。名字白名单（`YOUTUBE_ALLOWED_NAMES`）**只作用于 YouTube** —— twitter 分支不做名字过滤，所以**只要调用方传对 platform**，清洗逻辑就不会吃掉 `auth_token` / `ct0`。风险全在这一点上：`clean(x_cookies, "youtube")` 返回空列表。域名匹配只认"相等或子域"（裸 `.com` 不得匹配 `.x.com`），`drop_expired` 是与 `enable_cleaning` 无关的独立维度。


## 7. 后处理管道顺序

1. `SponsorBlockFeature` — sponsorblock_remove/mark
2. `MetadataFeature` — FFmpegMetadata 后处理器
3. `SubtitleFeature` — 语言解析、嵌入、清理
4. `ThumbnailFeature` — 通过 AtomicParsley (MP4) > FFmpeg (MKV) > mutagen (audio) 嵌入
5. `VRFeature` — EAC→Equi 转换 + 空间元数据（仅 VR 模式）

## 8. 测试规则

- pytest >= 7.0
- 测试文件在 `tests/` 目录
- 根目录 `conftest.py` 在收集测试前将应用数据和日志隔离到临时目录，并默认使用 Qt offscreen；部分测试仍自行设置 `sys.path`。
- 部分测试需要 `QApplication` —— 在**导入 fluentytdl 之前**设好 `QT_QPA_PLATFORM=offscreen` 与 `FLUENTYTDL_DATA_DIR_OVERRIDE` 即可无头运行（照抄 `tests/test_subtitle_selector_ux.py` 的文件头）。别写死"有几个 GUI 测试"，这个数字每加一个测试就过期
- CI 对 lint、格式、版本与锁文件、翻译同步及测试实行硬门禁；仅 Pyright 保持提示性质。
- 添加新测试时：优先使用普通 pytest 函数而非 unittest.TestCase

## 9. 禁止事项

- **不要**在 UI 中使用原始 Qt 控件（必须使用 QFluentWidgets）
- **不要**将 yt-dlp 作为 Python 库导入（始终使用 CLI 子进程）
- **不要**使用 `cookies_from_browser`（DPAPI 锁）
- **不要**强制 sleep interval（签名 URL 过期）
- **不要**在未记录于第 2 节的情况下创建新单例
- **不要**在未更新 `pyproject.toml` 的情况下添加依赖
- **不要**提交 `config.json`、凭证、API token 或 cookies
- **不要**随意使用 `type:ignore`
- **不要**绕过 `download/staging.py` 的 `StagingArea` 事务模型进行视频下载 —— 所有产物生命周期必须经由该 API 管理，不得绕行
- **不要**将 `outtmpl` 设为绝对路径，且落盘前所有路径必须通过 `assert_inside`（绝对 `-o` 会让 `-P` 整体失效，破坏沙盒重定向）
- **不要**在临界区之前（`reserve` 之前）对活事务 `rmtree` 沙盒，也不要在进入临界区后的任何一点取消事务 —— 唯一的取消门在 `commit()` 开头；`core/controller.py` 不得对活事务沙盒调用 `rmtree`
- **不要**在 `phase=committed` 之后将 outcome 改写为 `failed` —— 提交后的异常（`emit_actual`、`cleanup`）只能成为 `kind=signal`，不可逆的成功边界不可被后续异常推翻
- **不要**在任何提交前的门中消费 `delivered:*` token —— 它们在 `commit()` 之后才成立；`verify()` 中引用它们会让每个正常下载都被自己的安全门拦下
- **不要**用 `_commit_to_truth_source()` 以外的任何方式写 Cookie 真相源（§6）
- **不要**在 Qt 主线程调 `force_refresh_with_uac()` —— 统一走 `CookieRefreshWorker`（§6）
- **不要**把没核对过的 `platform` 传进 `CookieCleaner.clean()` 或 `_validate_cookies()` —— 传错会毁掉**另一个**平台的凭证（§6）

## 10. 关联文档

| 文档 | 用途 |
|------|------|
| `docs/ARCHITECTURE.md` | 当前架构（含 6 种解析流程详情） |
| `docs/YTDLP_KNOWLEDGE.md` | yt-dlp 经验排障知识库 |
| `docs/RULES_EN.md` | 本文档的英文版 |
| `CONTRIBUTING.md` | 贡献指南 |
| `SECURITY.md` | 安全策略 |

## 11. 打包与发布规则

- 统一 Windows x64、Python 3.12.12，工具链读取 build-environment.json。依赖使用 `uv sync --locked --extra dev --extra build`，构建和检查不得自动刷新锁文件。
- VERSION 保存裸版本；tag 为 v+VERSION。仅 version_manager set/bump 修改源码版本并刷新 uv.lock；build --version 只覆盖本次暂存输入。
- 每次发布必须拉取最新 yt-dlp、FFmpeg/ffprobe、Deno、AtomicParsley、POT Provider 和内嵌 7-Zip，沿用原渠道。TOOLS.lock.json 仅作历史参考，不阻止上游升级；下载校验失败禁止退回旧工具。
- 每次构建解析一次组件快照，记录资产标识、URL、版本、哈希与大小，供所有产物共用。回放快照仅供诊断，不允许发布。
- TARGET_OUTPUTS 是产物集合唯一事实源；正式发布必须 target=all。构建使用 build/runs/<id>，build/latest-result.json 仅指向成功构建。禁止按进程名全局强杀和删除历史发布物。
- app-core 白名单及污染排除来自 pyproject.toml；未知顶层项目和运行数据污染必须失败。portable.txt 只进入 Full；updater.exe.new 随 app-core 投递，失败保留重试材料。
- COPY/LZMA2 归档必须经真实解压与哈希比对，冻结 updater 在空 PATH、中文路径验证。冻结主程序必须通过隔离自检。卸载维护脚本随共享 _internal 分发，应用更新不能把它移除。
- 数据根不能混淆：便携配置/数据库通常在应用目录，安装版配置/数据库通常在 LocalAppData；账号仍在应用 bin/dle_user。保留现有路径和复制迁移机制，迁移标记仅在启动验收后提交。
- Inno 支持中英文，默认当前用户、可选所有用户，保留 AppId 和旧安装范围；首次启动语言不覆盖已有配置。
- **卸载彻底清理账号、Cookie、配置、数据库历史、缓存及日志，不提供保留选项；所有用户卸载覆盖实际使用者。** 下载成品及未知文件保留，不递归删除整个安装/下载/Documents 根，不跟随目录联接。清理失败必须报告失败。
- 覆盖安装、更新及回滚保留数据，不能调用卸载清理。
- stable 发布 Latest，rc 为预发布，beta 仅 Artifacts。发布验证 tag/提交/main 祖先关系、共用检查和产物，Draft 校验后原样公开并验证下载。禁止替换已公开版本资产。
- 具体命令和验收限制见 docs/build_release.md。
