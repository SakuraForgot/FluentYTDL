# yt-dlp 经验知识库

> [English version](YTDLP_KNOWLEDGE_EN.md)
>
> 每条记录遵循：**症状 → 根因 → 规则 → 代码引用**

## 1. 下载失败

### 1.1 下载中 403（签名过期）

**症状**：下载中途 HTTP 403，尤其长视频

**根因**：`sleep_interval` 在请求间引入延迟；YouTube 签名 URL 的 TTL 很短（~几分钟）

**规则**：绝对不要将 `sleep_interval_min` 或 `sleep_interval_max` 设置为非零值

**代码**：`src/fluentytdl/youtube/youtube_service.py` — AntiBlockingOptions 注释

### 1.2 年龄限制 / 会员专属内容 403

**症状**：yt-dlp 返回"请登录以确认年龄"或"此视频仅限会员"

**根因**：缺少或过期的 cookies；年龄限制内容需要已认证的会话

**规则**：CookieSentinel 自动检测这些关键词并触发 cookie 刷新流程

**代码**：`src/fluentytdl/auth/cookie_sentinel.py` — `COOKIE_ERROR_KEYWORDS`

### 1.3 Bot 检测（LOGIN_REQUIRED）

**症状**：yt-dlp 提取时返回 `LOGIN_REQUIRED` 错误

**根因**：YouTube 检测到自动化访问；需要 PO Token 或新 cookies

**规则**：POT Manager 提供 PO Token；如果不可用，回退到使用设置中静态 token 的 `mweb` client

**代码**：`src/fluentytdl/youtube/youtube_service.py` — `build_ydl_options()` 中的回退逻辑

### 1.4 "页面需要重新加载"错误

**症状**：yt-dlp 返回关于页面重新加载的错误

**根因**：过期的 cookies 或会话状态

**规则**：自动检测此错误并强制刷新 DLE cookies 进行单次重试

**代码**：`src/fluentytdl/youtube/youtube_service.py` — 提取方法中的重试逻辑

### 1.5 播放列表认证检查提示

**症状**：yt-dlp 提示关于 `youtubetab:skip=authcheck`

**根因**：YouTube 播放列表需要可以跳过的认证检查

**规则**：自动检测此提示并在重试时注入 `youtubetab:skip=authcheck` 作为 extractor-arg

**代码**：`src/fluentytdl/youtube/youtube_service.py` — 播放列表重试逻辑

### 1.6 子请求失败也打 `ERROR:`（任务其实没失败）

**症状**：单条字幕轨限流时，yt-dlp 打的是 `ERROR: Unable to download video subtitles for 'zh-Hans-en-GB': HTTP Error 429: Too Many Requests` —— 但它随后照旧下完别的语言、照旧正常收尾，退出码可以是 0

**根因**：那个 `ERROR:` 前缀描述的是**那一次子请求**，不是整个任务。诊断层若按前缀分层，就会把它和真正的致命错误摆在同一层里比优先级

**规则**：这类规则声明 `"appliesTo": "both"` 收下 `ERROR:` 行，再用 `"demoteToWarning": true` 把事件降回 warning 层。priority 不动 —— 它要留着在**同一行**上压过更泛化的规则（字幕 429 压 `rate_limited_429`），而级别决定它在跨行仲裁里的分层。少了降级，字幕规则的 p98 会把 `bot_check_sign_in`(p92) 这种真正的失败原因挤成配角；少了 `appliesTo: both`，真机上那行 `ERROR:` 连匹配的机会都没有，一路落到泛化的 `rate_limited_429`，字幕专属提示永远不出现

**代码**：`src/fluentytdl/diagnostics/engine.py::parse_events`、`assets/error_rules.json`

## 2. 格式选择

### 2.1 `-S lang:xx` 从来不表示"偏好某语言"

**症状**：格式排序中的 `-S lang:xx` 未能选择正确的音轨

**根因**（已对上游源码核实，早前记的"`language_preference=10` 覆盖了排序优先级"是**错的**）：`FormatSorter.settings` 里根本没有名为 `language` 的排序字段，`lang` 是 `language_preference` 的**数值**别名。喂它一个语言码时 `"ja".isnumeric()` 为假，于是 yt-dlp 改写**全局** `settings['lang']['convert'] = 'string'`、`limit="ja"`，排序键退化成拿 10/5/−1/−10 当字符串跟 `"ja"` 比。更要紧的是 `add_item` 有 `if field in self._order: return` —— 一串 `lang:` 条目里**只有第一个会被接受**，所以按 BCP-47 别名展开出十几条的做法贡献为零

**规则**：语言与原音偏好**只能**由格式串里的过滤器表达，走 `_inject_language_into_format()`：

- 语言用 `[language^=xx]`（startswith）。裸 `[language=en]` 匹配不到真实标注 `en-US`，那是"偏好了英语却一条音轨都拿不到"的直接原因；别名分支用精确 `=`（别名表把 `zh-Hans` 放宽到裸 `zh`，用 `^=` 展开会连 `zh-Hant` 一起命中）
- 原音用 `[language_preference>=?10]`。**`?`（none-inclusive）不能掉**：`_build_format_filter` 的 `_filter` 在 `actual_value is None` 时返回 `m.group('none_inclusive')`，不带 `?` 即为假，而 `language_preference` 只有 YouTube extractor 会算 —— Twitter 等平台会被过滤掉**所有**音轨。**位置也是语法的一部分**：该标记在运算符与值之间（`>=?10`）。写成值侧的 `>=10?` 解析不成数字，真实 yt-dlp 直接以 `SyntaxError: Invalid filter specification` 拒收整次运行（yt-dlp 2026.08.30 实测）
- 原始格式串永远作为最后一条兜底分支。少了它，没有对应音轨的视频会一条格式都选不出来（合并直接失败）
- 意图经 `ydl_opts["_fytdl_audio_langs"]` / `["_fytdl_audio_strategy"]` 传递（下划线前缀 = 不进 argv）；`format_sort` 只留 `res,br,fps,acodec`

**`language_preference` 的四个取值**（`extractor/youtube/_video.py::get_language_code_and_preference()` 计算，`-J` 输出里就有）：`10` 原音、`5` `audioIsDefault`（账号/地区默认，**不是**原音）、`-1` 普通配音、`-10` 音频描述轨。项目的音轨类型判定读的就是它（`format_scorer.audio_track_kind()`）

**代码**：`src/fluentytdl/youtube/yt_dlp_cli.py` — `_plan_language_injection()` / `_ORIGINAL_AUDIO_FILTER`；`tests/test_audio_format_injection.py` 是它的回归锁

### 2.2 BCP-47 别名扩展

**症状**：语言环境变体的语言匹配失败（音轨如 `zh-CN` vs `zh-Hans`；字幕如偏好 `en` 配不上真实键 `en-GB`）

**根因**：YouTube 在不同上下文中使用不同的语言环境代码。字幕更进一步：真实字幕键除了带地区（`en-GB`），自动生成/翻译的还是 `{目标}-{来源}` 复合键（`en-en-GB`、`zh-Hans-en-GB`）

**规则**：

- 音频：别名展开成格式串里的过滤器分支（见 §2.1）。**不要**往 format_sort 里塞 `lang:` 条目 —— 那条路早前被证伪，一个都不生效
- 字幕：**`--sub-langs` 的每一项被 yt-dlp 当作锚定正则**去匹配字幕键 —— 裸 `en` 匹配不到 `en-GB`，一个 `.vtt` 都不会写出来，而 yt-dlp 只留一句 `[info] There are no subtitles for the requested languages`。所以用户偏好必须先经 `bcp47.resolve_requested()` 解析成真实键；拿不到可用轨道列表时回落 `bcp47.to_sub_langs_pattern()`（`en(-.+)?`，**不是** `en.*` —— 后者会连带命中 `eng`/`enm` 这两个不同语种）
- 匹配是**单向**的：`en` 命中 `en-GB`，但 `en-GB` 不命中 `en`（用户点名要英国英语时，不该拿到一条不知道哪个地区的 `en`）
- 简繁**绝不能**混：`zh-Hans` 不得命中 `zh-Hant`

**代码**：`src/fluentytdl/utils/bcp47.py`（唯一权威；`utils/format_scorer.py` 只保留一层薄委托）

### 2.3 web_music 客户端需要 disable_innertube

**症状**：YouTube Music URL 的格式提取失败

**根因**：`web_music` 客户端的 InnerTube 挑战处理有缺陷

**规则**：使用 `web_music` player_client 时，始终在 PO Token 请求中设置 `disable_innertube=True`

**代码**：`src/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/extractor/getpot_bgutil_http.py`

### 2.4 不强制 player_client

**症状**：想强制使用 `android` 或 `ios` 客户端以获取更好的格式

**根因**：Android/iOS 模拟可能返回不完整的格式列表；yt-dlp 的默认策略（由实际内核版本及请求认证状态决定）经过充分测试

**规则**：绝对不要通过 extractor_args 强制 `player_client`；信任 yt-dlp 默认值

**代码**：`src/fluentytdl/youtube/youtube_service.py` — `build_ydl_options()` 中的注释

## 3. Windows 特有问题

### 3.1 .part-Frag 文件删除失败

**症状**：yt-dlp 返回退出码 1 但下载看起来已完成

**根因**：Windows 文件锁定阻止删除 `.part-Frag` 文件；下载实际已完成

**规则**：非零退出码时，检查输出文件是否存在且大小 >= 预期总字节数的 50%

**代码**：`src/fluentytdl/download/executor.py` — 预期大小验证

### 3.2 DPAPI Cookie 锁

**症状**：浏览器 cookie 提取挂起或失败；其他浏览器功能异常

**根因**：`--cookies-from-browser` 在 Windows 上通过 DPAPI 锁定 Chrome/Edge SQLite 数据库

**规则**：绝对不要使用 `--cookies-from-browser`；始终先通过 rookiepy 提取到文件

**代码**：`src/fluentytdl/auth/auth_service.py`

### 3.3 POT 插件发现失败

**症状**：编译后的 yt-dlp.exe 找不到 PO Token 提供者

**根因**：独立编译的 yt-dlp 不通过 PYTHONPATH 发现插件

**规则**：使用基于 mtime 的增量同步，将 POT 插件 `.py` 文件同步到 `<exe-dir>/yt-dlp-plugins/bgutil-ytdlp-pot-provider/`

**代码**：`src/fluentytdl/youtube/yt_dlp_cli.py` — `sync_pot_plugins_to_ytdlp()`

### 3.4 进程树终止

**症状**：取消后遗留孤立的 yt-dlp 或 ffmpeg 进程

**根因**：`terminate()` 只杀死直接子进程，不杀死衍生的子进程

**规则**：在 Windows 上，使用 `taskkill /F /T /PID` 杀死整个进程树

**代码**：`src/fluentytdl/download/executor.py` — 进程终止逻辑

### 3.5 跨驱动器文件移动失败

**症状**：`os.replace()` 在临时目录和目标位于不同驱动器时失败

**根因**：Windows 上 `os.replace()` 无法跨驱动器移动

**规则**：在目标目录同目录下创建临时文件，避免跨驱动器移动

**代码**：`src/fluentytdl/download/workers.py` — 沙箱目录创建

## 4. 网络 / 代理

### 4.1 TUN 模式双重代理

**症状**：系统 TUN/VPN（如 V2RayN）激活时下载失败或极慢

**根因**：注入 `HTTPS_PROXY`/`HTTP_PROXY` 环境变量导致流量同时经过 TUN 和代理

**规则**：检测到 TUN 模式时，不要向 POT Manager 子进程注入代理环境变量

**代码**：`src/fluentytdl/youtube/pot_manager.py` — 代理注入逻辑

### 4.2 代理关闭覆盖

**症状**：选择"无代理"时系统代理仍被使用

**根因**：系统级代理环境变量覆盖了 yt-dlp 设置

**规则**：代理模式为"off"时，显式设置 `proxy: ""` 以覆盖任何系统代理

**代码**：`src/fluentytdl/youtube/youtube_service.py` — `NetworkOptions`

### 4.3 Localhost 代理绕过

**症状**：POT Manager 的 HTTP 请求经过 TUN 代理而非 localhost

**根因**：`urllib.request.urlopen()` 遵循系统代理设置

**规则**：对 localhost 请求使用空的 `ProxyHandler` 以绕过 TUN 模式代理

**代码**：`src/fluentytdl/youtube/pot_manager.py` — `_local_urlopen()`

## 5. Cookie 系统

### 5.1 懒清理

**症状**：新提取成功前删除了旧 cookies → 认证空白期

**根因**：急切清理在替换验证前移除了可用的 cookies

**规则**：新提取成功并验证前，绝对不要删除旧 cookies

**代码**：`src/fluentytdl/auth/cookie_sentinel.py`

### 5.2 必需的 YouTube Cookies

**症状**：部分认证 — 某些功能正常，其他不正常

**根因**：缺少必需的 cookie 字段

**规则**：验证存在：SID、HSID、SSID、SAPISID、APISID

**代码**：`src/fluentytdl/auth/cookie_cleaner.py`

### 5.3 Chromium v130+ 应用绑定加密

**症状**：较新 Chrome/Edge 上 cookie 提取静默失败

**根因**：Chromium v130+ 使用应用绑定加密，需要管理员权限解密

**规则**：检测 Chromium 版本，需要时提示管理员提权

**代码**：`src/fluentytdl/auth/cookie_manager.py`

### 5.4 JSON Cookie 文件拒绝

**症状**：用户提供 JSON 格式的 cookies，yt-dlp 忽略它们

**根因**：yt-dlp 只接受 Netscape 格式

**规则**：检测 JSON cookie 文件并给出明确的警告信息

**代码**：`src/fluentytdl/youtube/youtube_service.py`

## 6. VR 视频

### 6.1 双通道提取

**症状**：VR 视频只显示低分辨率格式

**根因**：默认客户端不暴露高分辨率 VR 格式；需要 `android_vr` 客户端

**规则**：VR 模式始终使用 `extract_vr_info_sync()` 并设置 `player_client=["android_vr"]`

**代码**：`src/fluentytdl/youtube/youtube_service.py:1229`

### 6.2 EAC 到等矩形投影转换

**症状**：VR 视频在非 VR 播放器中以错误投影播放

**根因**：部分 YouTube VR 视频使用 EAC（等角立方体贴图）投影

**规则**：如果投影为 EAC 且自动转换已启用，运行 ffmpeg `v360=eac:e` 滤镜

**代码**：`src/fluentytdl/download/features.py:308` — `VRFeature.on_post_process()`

### 6.3 VR 检测启发式

**症状**：非 VR 视频被错误检测为 VR，或 VR 视频被遗漏

**根因**：VR 检测使用多个信号：标题关键词、格式元数据、分辨率异常

**规则**：检查投影字段、标签和标题中的关键词（360、VR、vr180、equirectangular）

**代码**：`src/fluentytdl/core/video_analyzer.py`

## 7. 沙箱下载模型

### 7.1 每个任务的临时目录

**症状**：取消时部分文件污染下载目录

**根因**：直接下载到最终目录在失败时留下碎片

**规则**：每个下载在 `.fluent_temp/task_<id>/` 中运行；仅成功后才移动文件到最终目录

**代码**：`src/fluentytdl/download/workers.py` — `DownloadWorker` 中的沙箱创建

### 7.2 取消清理延迟

**症状**：取消时沙箱目录未完全删除

**根因**：进程终止后 Windows 文件锁释放需要时间

**规则**：进程杀死后等待 1 秒再清理沙箱目录

**代码**：`src/fluentytdl/download/workers.py` — 取消清理逻辑
