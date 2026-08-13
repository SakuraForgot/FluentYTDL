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
- **暗色模式支持**：使用 `CustomInfoBar`，而非原始 InfoBar。
- **文字排版与字重**：严禁使用普通的 `BodyLabel` 作为标题或指令提示（会导致字体发虚）。必须使用 `StrongBodyLabel` 或 `SubtitleLabel`。
- **文字颜色对比度**：严禁硬编码使用 `Qt.GlobalColor.darkGray` 或 `QColor(160, 160, 160)`。次要文本（如 `CaptionLabel`）必须手动注入高对比度颜色：`setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))`，确保深色模式下清晰锐利。
- **SettingCard 安全修改**：严禁使用 Monkey Patch 全局修改 `SettingCard` 行为。自定义组件（如 `InlineComboBoxCard`）可能将 `contentLabel` 替换为无 `setTextColor` 方法的普通 `QLabel`，全局 Patch 会导致 `AttributeError`。应在页面级 `__init__` 中使用 `findChildren` 遍历，并结合 `hasattr` 进行安全处理。

### 文件命名

- 所有 Python 文件使用 snake_case
- 建议每个文件一个类（尤其在 ui/components/ 中）
- 私有模块级函数用 `_` 前缀

## 4. yt-dlp 集成规则 [关键]

这些规则来自生产环境的惨痛教训。违反它们**必然**导致用户可见的 bug。

1. **绝不强制 `player_client`** — 信任 yt-dlp 默认策略（tv → web_safari → android_vr）
2. **绝不启用 `sleep_interval`** — 导致签名 URL 过期 → HTTP 403
3. **绝不使用 `--cookies-from-browser`** — Windows 上导致 DPAPI 文件锁
4. **语言格式注入** — `-S lang:xx` 无法覆盖 `language_preference=10`；使用 `_inject_language_into_format()`
5. **非零退出时验证文件大小** — Windows `.part-Frag` 删除失败但下载已完成
6. **同步 POT 插件到 exe 目录** — 编译后的 yt-dlp 无法通过 PYTHONPATH 发现插件
7. **TUN 模式不注入代理环境变量** — 注入 `HTTPS_PROXY` 导致双重代理
8. **web_music 需要 `disable_innertube=True`** — 该客户端的 InnerTube 挑战有缺陷
9. **BCP-47 别名扩展** — `zh-Hans` 必须匹配 `zh-CN`、`zh-SG` 等
10. **沙箱下载模型** — 每个任务一个临时目录，成功后移动，取消时清理

详见 `docs/YTDLP_KNOWLEDGE.md` 完整经验知识库。

## 5. Cookie 系统规则

- `CookieSentinel` 管理单个 `bin/cookies.txt` 的生命周期
- **懒清理**：新提取成功前绝不删除旧 cookies
- **必需 cookies**：SID、HSID、SSID、SAPISID、APISID
- **Chromium v130+**：需要管理员权限进行应用绑定加密解密
- **403 错误恢复**：自动检测 Cookie 过期关键字，提示刷新
- **JSON Cookie 文件**：拒绝并警告（yt-dlp 仅支持 Netscape 格式）
- **WebView2 模式**：WebView2 是项目内基于 WebView2 的 Cookie 提取机制的新名称（以前称为 DLE）。在代码中，必须始终使用 `AuthSourceType.WEBVIEW2` 和术语 `webview2`。在 UI 文本中，使用 `登录获取 (WebView2)` 或 `WebView2 登录`。**绝对不要**将其重新命名回 `DLE`。
- **WebView2 提取器**：当前实现使用 `WebView2CookieProvider`。在代码中引用时使用 `WebView2CookieProvider`，在用户界面中使用 `WebView2`。

## 6. 后处理管道顺序

1. `SponsorBlockFeature` — sponsorblock_remove/mark
2. `MetadataFeature` — FFmpegMetadata 后处理器
3. `SubtitleFeature` — 双语合并、嵌入、清理
4. `ThumbnailFeature` — 通过 AtomicParsley (MP4) > FFmpeg (MKV) > mutagen (audio) 嵌入
5. `VRFeature` — EAC→Equi 转换 + 空间元数据（仅 VR 模式）

## 7. 测试规则

- pytest >= 7.0
- 测试文件在 `tests/` 目录
- **尚无 conftest.py** — 每个测试自行设置 `sys.path`
- 2 个测试需要 GUI（QApplication）— 无法在无头 CI 中运行
- 1 个测试无断言（test_error_parser.py）— 需要修复
- CI 所有检查使用 `continue-on-error: true` — 没有阻塞合并的检查
- 添加新测试时：优先使用普通 pytest 函数而非 unittest.TestCase

## 8. 禁止事项

- **不要**在 UI 中使用原始 Qt 控件（必须使用 QFluentWidgets）
- **不要**将 yt-dlp 作为 Python 库导入（始终使用 CLI 子进程）
- **不要**使用 `cookies_from_browser`（DPAPI 锁）
- **不要**强制 sleep interval（签名 URL 过期）
- **不要**在未记录于第 2 节的情况下创建新单例
- **不要**在未更新 `pyproject.toml` 的情况下添加依赖
- **不要**提交 `config.json`、凭证、API token 或 cookies
- **不要**随意使用 `type:ignore`
- **不要**绕过沙箱下载模型进行视频下载

## 9. 关联文档

| 文档 | 用途 |
|------|------|
| `docs/ARCHITECTURE.md` | 当前架构（含 6 种解析流程详情） |
| `docs/YTDLP_KNOWLEDGE.md` | yt-dlp 经验排障知识库 |
| `docs/RULES_EN.md` | 本文档的英文版 |
| `CONTRIBUTING.md` | 贡献指南 |
| `SECURITY.md` | 安全策略 |

## 10. 打包与发布规则

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
- `--target` 可选值：`all`、`7z`（或 `full`）、`setup`
- **`build.py` 仅在显式传入 `--version` 时才回写 `VERSION`。** 不传 `--version` 的构建绝不会篡改真相源。
- 向 `build.py` / `version_manager.py set` 传入带 `v` 前缀的版本会被拒绝并给出纠正提示。

### 发布产物

| 产物 | 面向对象 |
| --- | --- |
| `FluentYTDL-{VERSION}-win64-full.7z` | **首要推荐** —— 便携免安装，解压即用，内置全部 `bin/` 工具 |
| `FluentYTDL-{VERSION}-win64-setup.exe` | Inno Setup 安装向导 —— 写注册表、建快捷方式，需要管理员权限 |
| `FluentYTDL-{VERSION}-win64-app-core.7z` | **内部包** —— 供程序内自动更新使用的增量载荷，不含 `bin/` 与 `updater.exe`，单独解压无法运行；**绝不可**作为用户下载项展示 |
| `update-manifest.json` | 程序内更新器通过 `releases/latest/download/` RAW 直链消费 |
| `SHA256SUMS.txt` | 上述全部产物的完整性校验 |

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
