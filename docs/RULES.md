# FluentYTDL 开发规则

> [English version](RULES_EN.md)

## 1. 项目身份

- **名称**：FluentYTDL — 专业 YouTube/视频下载器
- **语言**：Python 3.10+
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
target-version = "py310"
line-length = 100
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]  # 允许长行
```

- `__init__.py` 文件中忽略 `F401`（重导出是有意的）
- isort：`known-first-party = ["fluentytdl"]`

### Pyright（建议性）

```toml
pythonVersion = "3.10"
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

1. **优先使用 yt-dlp 默认的 `player_client` 策略**（tv → web_safari → android_vr）——绝不*锁定*单一客户端。**例外（SABR-only 账号）：** 当 yt-dlp 报告账号命中 SABR-only 实验（`forcing SABR streaming` / `formats ... missing a url` → 高清格式没有直链、被丢弃，只剩 360p）时，向客户端集合**追加** `web_safari`（`default,web_safari`，或已设 POT 时的 `<现有>,web_safari`，例如 `default,mweb`）。这是*追加*而非锁定——默认客户端仍先跑，web_safari 只是找回直链高清（HLS）的兜底。在解析期侦测（`yt_dlp_cli._maybe_mark_sabr_only`），持久化**在账号上**（`WebView2Account.sabr_only`，登出时用内存会话标志），并在 `build_ydl_options` 中消费（`_maybe_append_sabr_web_safari`），从而同时覆盖解析**与**下载两条 opts 路径（各自独立构建 opts）。此为临时方案——随着采样更多视频可能需要调整。
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
- **尚无 conftest.py** — 每个测试自行设置 `sys.path`
- 部分测试需要 `QApplication` —— 在**导入 fluentytdl 之前**设好 `QT_QPA_PLATFORM=offscreen` 与 `FLUENTYTDL_DATA_DIR_OVERRIDE` 即可无头运行（照抄 `tests/test_subtitle_selector_ux.py` 的文件头）。别写死"有几个 GUI 测试"，这个数字每加一个测试就过期
- CI 所有检查使用 `continue-on-error: true` — 没有阻塞合并的检查
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

### 版本管理

- **唯一真相源**：项目根目录的 `VERSION` 文件，存**裸版本号**，**不带 `v` 前缀**
- **不要手改** `__init__.py`、`pyproject.toml`、`FluentYTDL.iss` 里的版本号 —— 一律走 `scripts/version_manager.py`
- Git tag 恒为 `"v" + VERSION` —— `v` 只存在于 tag，不存在于文件里

### 版本格式（PEP 440 / SemVer）

```text
MAJOR.MINOR.PATCH[-(rc|beta).N]
```

| VERSION 文件 | Git tag | 通道 | 分发方式 |
| --- | --- | --- | --- |
| `3.5.5` | `v3.5.5` | stable | GitHub Release (Latest) —— 接收程序内自动更新 |
| `3.5.6-rc.1` | `v3.5.6-rc.1` | rc | GitHub Release (Pre-release) —— 自动更新 **locked** |
| `3.6.0-beta.1` | `v3.6.0-beta.1` | beta | 仅 Artifacts，群/频道分发 —— 自动更新 **locked** |

- **不再使用前缀。** 旧的 `v-` / `pre-` / `beta-` 前缀体系已废弃。运行时代码仍能*读取*这些格式，用于兼容 3.5.5 之前的安装，但不会再写出。
- `3.5.6-rc.1` 是合法 PEP 440（规范化为 `3.5.6rc1`），因此 `pyproject.toml` 存完整版本号。
- **Inno Setup / PE 资源只接受纯数字版本。** `FluentYTDL.iss` 存数字段（`3.5.6`），其 `MyAppVersionNumeric` 宏会在第一个连字符处截断。
- 预发布通道只认 `rc` 和 `beta`。`alpha`、裸 `-rc`、`3.5.5rc1` 一律拒绝。

### AI Agent：发布流程

**正式版**：

1. `python scripts/version_manager.py set 3.5.6`
2. `python scripts/version_manager.py check`（校验 4 个文件一致）
3. `git add -A && git commit -m "release: v3.5.6"`
4. `git tag v3.5.6`
5. `git push && git push --tags`
6. CI 自动触发 `release.yml` → 构建 → GitHub Release (Latest)

**预发布 (rc)**：`python scripts/version_manager.py set 3.5.6-rc.1`（或 `bump patch --pre rc`），之后同上 2-5 步，tag 为 `v3.5.6-rc.1` → GitHub Release 标记为 Pre-release。

**测试版 (beta)**：`python scripts/version_manager.py set 3.6.0-beta.1`，之后同上 2-5 步 → 仅产出 Artifacts，不创建 GitHub Release，由项目负责人从 GitHub Actions Artifacts 下载分发。

### 本地构建

- GUI：`python scripts/build_gui.py` → 版本框**留空**即使用 `VERSION` → 点击构建
- CLI：`python scripts/build.py --target all`（版本从 `VERSION` 读取）
- **`build.py` 仅在显式传入 `--version` 时才回写 `VERSION`。** 不传 `--version` 的构建绝不会篡改真相源。
- 向 `build.py` / `version_manager.py set` 传入带 `v` 前缀的版本会被拒绝并给出纠正提示。

### 构建目标 [关键]

**目标 → 产出物的映射唯一事实源是 `scripts/build.py` 的 `TARGET_OUTPUTS`。** `run_all()` 的分发、`_assert_expected_artifacts()`、`build_gui.py` 的产出面板、release.yml 的校验步骤全都读这一张表，**不要写第二份**。

| `--target` | 产出 |
| --- | --- |
| `all` | `full.7z` + `app-core.7z` + `setup.exe` + `update-manifest.json` + `SHA256SUMS.txt` |
| `7z`（或 `full`） | **只有** `full.7z` |
| `app-core` | `app-core.7z` + `update-manifest.json` |
| `setup` | **只有** `setup.exe` |
| `spec` | 不产出发布物，只验证 PyInstaller 蓝图后返回 |

- **目标是严格的：目标没点名的一律不生成。** 以前这里是三处各写一遍 `if target in ("all", "7z")`，加上无条件生成清单与校验和，于是「只要便携版」照样吐出 `app-core.7z`、`update-manifest.json`，以及一份把 `release/` 里所有历史遗留文件都列进去的 `SHA256SUMS.txt`。新增目标是往表里加一行，不是再加一个 `if`。
- **`manifest` 绑定 `app-core`，绝不单独产出。** 清单里唯一有实质内容的组件就是 app-core 归档，缺了它 `generate_manifest.py` 只会打印「⚠ app-core 归档不存在」并写出一份空壳清单 —— 那种清单一旦发布，程序内更新器会认为新版本无可下载载荷。宁可没有清单，也不要空壳清单。
- **`generate_checksums()` 只哈希本次构建的产物**，不再遍历整个 `release/`。以前遍历目录，这正是「只打便携版」却发出一份点名了其他版本包的校验文件的原因。
- **`clean()` 从不清 `release/`。** 历史产物按设计原地保留（那是本地唯一副本），所以按目标构建时自己的产物旁边必然躺着无关文件。`build_gui.py` 会把它们标为「历史遗留」并提供「构建前清空 `release/`」勾选项；`_warn_foreign_release_files()` 只点名，绝不删除。
- **`--print-names --target X` 只打印该目标的预期文件名后退出，不构建。** release.yml 的校验步骤向它索取清单而不是硬编码一份 —— 那份硬编码清单正是目标收紧后立刻变成误报的东西。
- **`publish=true` 只允许 `targets=all`**（在 release.yml 的版本解析步骤里拦截）。非 `all` 目标填不满 Release 正文里的下载链接，也不产出 `update-manifest.json`，发出去会中断所有已安装用户的自动更新。标签推送始终强制 `all`。

### 发布产物

| 产物 | 面向对象 |
| --- | --- |
| `FluentYTDL-{VERSION}-win64-full.7z` | **首要推荐** —— 便携免安装，解压即用，内置全部 `bin/` 工具 |
| `FluentYTDL-{VERSION}-win64-setup.exe` | Inno Setup 安装向导 —— 写注册表、建快捷方式，需要管理员权限 |
| `FluentYTDL-{VERSION}-win64-app-core.7z` | **内部包** —— 供程序内自动更新使用的增量载荷，不含 `bin/` 与 `updater.exe`，单独解压无法运行；**绝不可**作为用户下载项展示 |
| `update-manifest.json` | 程序内更新器通过 `releases/latest/download/` RAW 直链消费 |
| `SHA256SUMS.txt` | 完整性校验 —— 只覆盖**本次构建**的产物（`--target all`） |

资产下载 URL 以 **tag** 而非版本号为键 —— `generate_manifest.py` 的 `--tag` 参数正是为此存在（`/releases/download/v3.5.5/FluentYTDL-3.5.5-win64-full.7z`）。

### 打包卫生 [关键]

- **`pyproject.toml [tool.fluentytdl.build]` 是"发布物包含什么"的唯一事实源。** `app_core_include`（白名单）、`app_core_exclude`（已知且故意不收）、`dist_forbidden`（运行期垃圾黑名单）只写在这里。`dist/` 顶层出现两张名单都没登记的条目时 `classify_app_core_items()` 直接让构建失败 —— 白名单真正的风险是"以后新增的合法发布物被静默丢掉"，这条断言把它变成一盏红灯。每个数组都必须写成**单行**：`_load_config()` 在没有 `tomllib` 的 Python 3.10 上会退化成只认 `key = [...]` 的行解析器，多行数组会解析成空数组，从而静默地让整道检查失效。
- **`assert_dist_clean()` 对三个发布目标全都跑**（`full.7z`、`app-core.7z`、`setup.exe`），不是只跑一个。任何人从 `dist/` 直接启动过程序，自己的 `config.json`、`logs/`、`state/tasks/tasks.db` 就留在了那里，而 `bin/cookies_*.txt` 与 `bin/dle_user/` 里是**真实凭据** —— 这些进了公开归档是会话泄漏，不是观感问题。`full.7z` 合法地包含 `bin/` 与 `updater.exe`，套不了 app-core 的白名单，兜住它的正是这份黑名单。
- **构建 `updater.exe` 需要 build extra：`uv sync --extra build`。** `py7zr` 是 updater 解压 app-core 归档的唯一手段。钉住的版本必须三处一致 —— `pyproject.toml` 的 `build` extra、`.github/workflows/release.yml` 的 `PY7ZR_VERSION`、以及 `scripts/updater.spec` 里那道断言。
- **`updater.exe.new` 随 app-core 投递，app-core 里没有 `updater.exe`。** 用户机器上正在运行的 `updater.exe` 覆写不了自己，所以修复只能以"归档里一个普通文件"的形式送到已安装用户手上：`build_updater()` 把产物额外拷成 `dist/updater.exe.new`，真正的替换由 `main.py::_cleanup_update_residuals()`（便携版 / 可写安装路径）或提权 updater 退出后的 helper `updater.py::_self_update_updater()`（Program Files）完成。两条路径互为兜底 —— 替换失败时**绝不要删掉** `updater.exe.new`，它就是下次重试的素材。

### 数据位置 [关键]

`utils/paths.py::user_data_dir()` 用**双轨**决定数据根目录，**绝不做写权限探测**：

| 场景 | 位置 |
| --- | --- |
| 传了 `--data-dir` / `FLUENTYTDL_DATA_DIR_OVERRIDE` | 该路径（updater 降权重启新版时用） |
| frozen 且 exe 同级有 `portable.txt` | exe 所在目录（便携版 `full.7z`） |
| frozen 且无标记 | `%LOCALAPPDATA%\FluentYTDL`（安装版） |
| 非 frozen | `project_root()` |

- **绝不要重新引入 `.writetest` 写探测。** 同一台机器的数据分裂成两棵树就是它造成的：提权会话写得进 `C:\Program Files\FluentYTDL`，普通会话写不进，用户看到的就是"更新把我的设置和任务全弄没了"。
- **`portable.txt` 只进 `full.7z`**，由 `create_7z()` 从 `tempfile.TemporaryDirectory()` 追加。绝不能写进 `dist/` —— `dist/` 是 app-core 与 `setup.exe` 的共同取材地，`dist_forbidden` 里列着它，写进去会直接打断构建。`.iss` 另有 `Excludes: "portable.txt"` 作为纯保险。
- **迁移只复制、绝不删除遗留位置**（`migrate_user_data()`），因为二进制回滚必须等价于数据兼容的回滚。`.migrated_v2` 标记只由 `finalize_startup()` → `commit_migration_marker()` 写出，且只在本次零失败时写 —— 写早了，被回滚的旧版会继续往旧路径写数据，而下次更新看到标记就跳过迁移、直接采用陈旧副本。
- **`paths.py` 永远不能 import loguru。** `utils/logger.py:13` 在导入期就求值 `LOG_DIR = str(user_data_dir() / "logs")`，反向 import 会成环；迁移消息先攒在模块级列表里，由 `utils/startup_info.py::log_startup_info()` 回放。

### 注意事项

- `build.py` 构建前会把版本同步到 `pyproject.toml`、`__init__.py`、`.iss`；当 `__init__.py` 动态读取 `VERSION` 时跳过同步
- 产物文件名带的是裸版本号，不是 tag：`FluentYTDL-3.5.5-win64-full.7z`
- 缺失 ISCC 或 `.iss` 属于**硬失败** —— 构建绝不会在零产物的情况下报成功
