<p align="center">
  <img src="assets/logo.png" alt="FluentYTDL 标志" width="128" height="128">
</p>

<h1 align="center">FluentYTDL</h1>

<p align="center">
  <strong>在 Windows 上更清晰地下载、管理与处理媒体。</strong><br>
  <sub>原生 Fluent Design · YouTube 与 X · 由 yt-dlp 和 FFmpeg 驱动</sub>
</p>

<p align="center">
  <a href="README.md">🌐 English</a> ·
  <a href="#-功能">✨ 功能</a> ·
  <a href="#-安装">📦 安装</a> ·
  <a href="#-开发">🧑‍💻 开发</a> ·
  <a href="docs/ARCHITECTURE_CN.md">🏗️ 架构</a>
</p>

<p align="center">
  <a href="https://github.com/SakuraForgot/FluentYTDL/releases/latest"><img src="https://img.shields.io/github/v/release/SakuraForgot/FluentYTDL?style=flat-square&color=7c3aed&label=release" alt="最新版本"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-2563eb.svg?style=flat-square" alt="GPL-3.0 许可证"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10 或更高版本">
  <img src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  <a href="https://github.com/SakuraForgot/FluentYTDL/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/SakuraForgot/FluentYTDL/ci.yml?branch=main&style=flat-square&label=CI" alt="CI 状态"></a>
</p>

<p align="center">
  <a href="https://github.com/SakuraForgot/FluentYTDL/stargazers"><img src="https://img.shields.io/github/stars/SakuraForgot/FluentYTDL?style=flat-square&logo=github&color=f59e0b&label=stars" alt="GitHub Stars"></a>
  <a href="https://github.com/SakuraForgot/FluentYTDL/releases"><img src="https://img.shields.io/github/downloads/SakuraForgot/FluentYTDL/total?style=flat-square&logo=github&color=0ea5e9&label=downloads" alt="Release 总下载量"></a>
  <a href="https://github.com/SakuraForgot/FluentYTDL/releases/latest"><img src="https://img.shields.io/github/downloads/SakuraForgot/FluentYTDL/latest/total?style=flat-square&logo=github&color=8b5cf6&label=latest%20downloads" alt="最新版下载量"></a>
</p>

FluentYTDL 将 yt-dlp 与 FFmpeg 整合为有引导的桌面工作流：解析链接、精确选择所需内容、在一个队列中跟踪所有任务，并由应用协调下载、恢复与后处理，无需手工拼接命令行参数。

> [!NOTE]
> FluentYTDL 面向 **64 位 Windows 10 和 Windows 11** 开发。YouTube 拥有最完整的功能覆盖；X 目前支持单条帖子链接，个人主页和时间线下载不在正式支持范围内。

## 🌟 项目影响力

FluentYTDL 让偏好图形界面的 Windows 用户也能轻松使用 yt-dlp 与 FFmpeg 的进阶工作流。官方 Release 资产累计下载量已超过 **20,000 次**，项目持续根据真实 Issue 推进版本发布，并跟进上游平台与工具变化。

---

## 🪟 界面预览

| 解析 | 格式选择 | 任务队列 |
|:--:|:--:|:--:|
| <img src="docs/images/reference/1_quick_start/主程序界面-精确解析.png" alt="解析页面" width="460"> | <img src="docs/images/reference/1_quick_start/精确解析解析页.png" alt="格式选择窗口" width="330"> | <img src="docs/images/reference/1_quick_start/任务列表.png" alt="任务队列" width="460"> |

## ✨ 功能

| | |
| --- | --- |
| 🎚️ **精确格式控制**<br>选择分辨率、容器、编码、音轨语言，或直接使用简易画质预设。 | 📚 **批量工作流**<br>解析 YouTube 播放列表与频道，按需加载信息并将所选内容加入队列。 |
| 🧠 **可靠任务管理**<br>持久化任务状态、隔离临时文件、配置并发，并在重启后恢复任务。 | 🛡️ **画质守卫**<br>检测格式降级，并可通过 FFprobe 验证下载后的媒体信息。 |
| 💬 **字幕与封面**<br>独立下载、合并双语字幕，并嵌入支持的封面与元数据。 | ✂️ **视频裁切**<br>为普通 YouTube 单视频选择时间范围，支持快速或精确裁切。 |
| 🎞️ **媒体处理**<br>使用 FFmpeg 合并与转换、处理 SponsorBlock 片段，并可转换 VR 投影。 | 🔐 **会话验证**<br>管理本地 Cookie，并在网站需要会话时使用隔离的 WebView2 登录流程。 |
| 🧰 **组件管理**<br>通过校验和验证应用与外部媒体工具更新。 | 🪟 **原生桌面体验**<br>在专注的 Fluent 界面中完成原本需要多个命令行工具配合的流程。 |

## 🧭 支持范围

| 范围 | 当前支持情况 |
|---|---|
| 🪟 操作系统 | Windows 10/11，64 位 |
| 🌐 应用语言 | 简体中文和英文，重启后生效；音轨和字幕语言独立选择。详见[语言与日志策略](docs/I18N.md)。 |
| ▶️ YouTube | 视频、Shorts、直播/视频链接、播放列表、频道、字幕、封面和 VR 工作流 |
| 𝕏 X（Twitter） | 含可下载媒体的单条帖子链接 |
| 🌐 其他 yt-dlp 网站 | 不属于当前正式支持范围 |
| 🐍 源码运行 | Python 3.10 或更高版本 |

> [!IMPORTANT]
> FluentYTDL 依赖上游网站与工具。网站改动、媒体 URL 过期、账号或地区限制以及 yt-dlp 回归，都可能暂时影响解析或下载。

---

## 📦 安装

### 🪟 Windows 即用构建包

从 **[GitHub Releases](https://github.com/SakuraForgot/FluentYTDL/releases/latest)** 下载最新安装包或便携包。

| 构建包 | 适用场景 |
| --- | --- |
| `*-setup.exe` | 标准 Windows 安装 |
| `*-full.7z` | 包含完整运行环境的便携版本 |
| `*-app-core.7z` | 仅更新应用核心 |

> [!TIP]
> 发布资产同时提供 `SHA256SUMS.txt`。重新分发或长期归档时建议验证校验和。官方构建仅通过本仓库发布。

### 🛠️ 从源码运行

```powershell
git clone https://github.com/SakuraForgot/FluentYTDL.git
cd FluentYTDL

# 推荐使用 uv
uv sync --extra dev
uv run python main.py
```

也可以创建 Python 虚拟环境，运行 `pip install -e ".[dev]"`，然后使用 `python main.py` 启动。

> [!NOTE]
> 程序会管理 yt-dlp、FFmpeg、Deno、AtomicParsley 等外部组件。源码工作区首次使用对应功能时，可能会下载缺失组件。

---

## 🧑‍💻 开发

| 层级 | 实现 |
| --- | --- |
| 🖼️ 桌面界面 | PySide6 与 PySide6-Fluent-Widgets |
| 🐍 应用核心 | Python 3.10+，采用 `src/` 包布局 |
| ⬇️ 下载引擎 | 位于 CLI 子进程边界后的 yt-dlp |
| 🎞️ 媒体管线 | FFmpeg 与配套媒体工具 |
| 🧱 服务边界 | 账号验证、下载、处理、存储与 YouTube 服务 |

```powershell
uv sync --extra dev
uv run ruff check src tests

$env:QT_QPA_PLATFORM = "offscreen"
uv run pytest tests -q

uv run python scripts/version_manager.py check
```

### 📚 文档地图

| 文档 | 适合何时阅读 |
| --- | --- |
| [贡献指南](CONTRIBUTING.md) | 环境配置、开发流程、测试与 Pull Request 要求 |
| [架构说明](docs/ARCHITECTURE_CN.md) | 模块边界与应用结构 |
| [开发规则](docs/RULES.md) | 本仓库特有的工程约束 |
| [yt-dlp 集成知识](docs/YTDLP_KNOWLEDGE.md) | 子进程协议与下载器行为 |
| [安全政策](SECURITY.md) | 支持版本与私密漏洞报告流程 |

## 🔐 安全与合理使用

> [!CAUTION]
> Cookie、日志、下载内容和本地配置可能包含敏感信息。请勿在 Issue 中公开未脱敏的 Cookie、凭据、Token 或私人链接。安全漏洞请通过 [SECURITY.md](SECURITY.md) 中的私密流程报告。

> [!IMPORTANT]
> 请仅下载你有权访问和保存的内容。用户有责任遵守适用法律、著作权规则和平台条款。本项目不会为用户提供访问私人或受限内容的权限。

## 🤝 贡献与治理

欢迎可复现的错误报告、范围明确的 Pull Request、文档改进和平台反馈。

| | 项目文档 |
| --- | --- |
| 🛠️ | [贡献指南](CONTRIBUTING.md) |
| 🫶 | [行为准则](CODE_OF_CONDUCT.md) |
| 🧑‍🚀 | [维护者说明](MAINTAINERS.md) |
| 🛡️ | [安全政策](SECURITY.md) |

> [!NOTE]
> 源代码使用 [GPL-3.0](LICENSE)。[商标政策](TRADEMARK.md)保护项目身份但不减少 GPL 授予的权利；[学术诚信说明](ACADEMIC_HONESTY.md)解释署名要求，不附加许可证限制。

---

## 💛 致谢

FluentYTDL 受益于出色的开源生态。特别感谢以下项目，以及让核心体验成为可能的每一位维护者：

| | 项目 | 为 FluentYTDL 提供的能力 |
| --- | --- | --- |
| 🎬 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 媒体解析、格式发现与下载编排。 |
| 🎞️ | [FFmpeg](https://ffmpeg.org/) | 音视频流合并、格式转换、元数据处理与 VR 投影支持。 |
| 🖥️ | [PySide6](https://doc.qt.io/qtforpython-6/) 与 [PySide6-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) | Qt 桌面基础与 Fluent 风格界面。 |
| 🔐 | [rookiepy](https://github.com/thewh1teagle/rookiepy) 与 [pywebview](https://github.com/r0x0r/pywebview) | 浏览器会话集成与隔离的 WebView2 登录体验。 |
| 🧩 | [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) 与 [Deno](https://github.com/denoland/deno) | YouTube 播放器兼容与 JavaScript 挑战支持。 |
| ✨ | [SponsorBlock](https://sponsor.ajay.app/) 与 [AtomicParsley](https://github.com/wez/atomicparsley) | 赞助片段处理与媒体元数据增强。 |

### 🌱 社区

感谢每一位上游维护者、[代码贡献者](https://github.com/SakuraForgot/FluentYTDL/graphs/contributors)、问题反馈者、文档贡献者和测试者。每一份可复现的报告与用心的改进，都在帮助 FluentYTDL 变得更加可靠、易用。

📜 许可证文本与第三方声明统一收录于 [`licenses/`](licenses/)。如果这个项目对你有所帮助，也欢迎关注并支持让它成为可能的上游项目。
