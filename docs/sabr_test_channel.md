# SABR 测试核心频道

自 3.7.7-rc.1 起，新配置默认选择「SABR Test (测试核心)」，Full 便携版和安装包内置经校验的 SABR 核心。已有配置中的频道和自定义路径保持原选择；软件 stable/pre 更新通道与核心频道分别设置。

应用内更新包不包含工具。旧用户可在设置页的组件区域，将「yt-dlp 更新频道」选为「SABR Test (测试核心)」，阅读提示并确认，再点击 yt-dlp 引擎卡片的「立即更新」。取消弹窗会恢复原频道。切回 Stable / Nightly / Master 后，仍通过该卡片下载安装对应官方核心。

VR 入口复用普通视频解析，仅在投影证据不足时补充 android_vr 探测；下载按实际格式能力选择客户端并合并音视频流。测试核心的直播解析仍暂时受限。

继续沿用同名 `yt-dlp.exe`、原有组件安装目录和 CLI 调用方式。POT Provider、插件同步、Deno、FFmpeg 和打包工具链保持原有机制。自定义核心路径仍优先；托管目录更新不会覆盖或自动切换用户的外部核心，组件卡片继续显示实际使用路径。

## 发布来源与版本校验

- 来源为 [SakuraForgot/yt-dlp-sabr-fix](https://github.com/SakuraForgot/yt-dlp-sabr-fix) 的公开 Release。
- 从 Release 列表按发布时间选择完整的 `sabr-*` 发布，包含 prerelease，忽略草稿及缺少必要资产的条目。不会使用不包含 prerelease 的 `/releases/latest`。
- 用同一 Release 的 `BUILD_INFO.json` 检查频道、架构、源码 SHA、显示版本和 EXE SHA256；如 GitHub 提供资产 digest，要求两者一致。
- 构建同样按发布时间选择最新完整发布，核对 `SHA256SUMS.txt`、核心实测版本及 SABR 身份，保留 `BUILD_INFO.json` 和许可证；各发布包共享同一次组件快照，失败不回退官方或旧缓存。
- 使用现有 HTTPS/代理传输与组件更新子进程下载，校验 SHA256 后才替换。测试仓库直接访问 GitHub，不走尚未提供该频道的组件镜像。
- 以实际 EXE 的版本信息判断当前频道，以实际文件哈希区分同一天的不同构建。测试源失败时不会自动安装官方核心。
- 切换频道后丢弃旧频道尚未返回的检查结果，安装入口也会拒绝旧频道缓存链接。安装过程中禁止再次切换频道。

## 本次远端发布

- [Actions](https://github.com/SakuraForgot/yt-dlp-sabr-fix/actions/runs/36419057785)
- [Release](https://github.com/SakuraForgot/yt-dlp-sabr-fix/releases/tag/sabr-6ef0ae00f0a4-10a9f1a4961d-r36419057785-1)
- EXE SHA256：`04cd74c7266add6f6b4258987a0847acb191822b5f83fae2ac3cb992420f0c75`
- 源码 SHA：`6ef0ae00f0a4e9dd042193b3f5a2bb28b5fc0ca6`
- 显示版本：`2026.09.28`

已在隔离目录用真实组件检查器和更新子进程验证官方 → SABR → 官方切换，两侧均通过哈希校验、版本探测及安装后复查。弹窗确认/取消、频道竞争、同日构建识别、更新器和翻译等相关测试共 372 项通过。

应用进度解析同时兼容 SABR 并行下载的 `1: ` / `2: ` 前缀；不改动 SABR 上游实现。直播、限速、分段下载等仍受上游支持范围限制。FluentYTDL 源码不会复制到核心构建仓库。
