<p align="center">
  <img src="assets/logo.png" alt="FluentYTDL logo" width="128" height="128">
</p>

<h1 align="center">FluentYTDL</h1>

<p align="center">
  <strong>A clearer way to download, organize, and process media on Windows.</strong><br>
  <sub>Native Fluent Design · YouTube and X · Powered by yt-dlp and FFmpeg</sub>
</p>

<p align="center">
  <a href="README.zh-CN.md">🌐 简体中文</a> ·
  <a href="#-features">✨ Features</a> ·
  <a href="#-installation">📦 Installation</a> ·
  <a href="#-development">🧑‍💻 Development</a> ·
  <a href="docs/ARCHITECTURE_EN.md">🏗️ Architecture</a>
</p>

<p align="center">
  <a href="https://github.com/SakuraForgot/FluentYTDL/releases/latest"><img src="https://img.shields.io/github/v/release/SakuraForgot/FluentYTDL?style=flat-square&color=7c3aed&label=release" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-2563eb.svg?style=flat-square" alt="GPL-3.0 license"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12 or newer">
  <img src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  <a href="https://github.com/SakuraForgot/FluentYTDL/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/SakuraForgot/FluentYTDL/ci.yml?branch=main&style=flat-square&label=CI" alt="CI status"></a>
</p>

<p align="center">
  <a href="https://github.com/SakuraForgot/FluentYTDL/stargazers"><img src="https://img.shields.io/github/stars/SakuraForgot/FluentYTDL?style=flat-square&logo=github&color=f59e0b&label=stars" alt="GitHub stars"></a>
  <a href="https://github.com/SakuraForgot/FluentYTDL/releases"><img src="https://img.shields.io/github/downloads/SakuraForgot/FluentYTDL/total?style=flat-square&logo=github&color=0ea5e9&label=downloads" alt="Total release downloads"></a>
  <a href="https://github.com/SakuraForgot/FluentYTDL/releases/latest"><img src="https://img.shields.io/github/downloads/SakuraForgot/FluentYTDL/latest/total?style=flat-square&logo=github&color=8b5cf6&label=latest%20downloads" alt="Latest release downloads"></a>
</p>

FluentYTDL turns yt-dlp and FFmpeg into a guided desktop workflow. Parse a link, choose exactly what you need, follow every task from one queue, and let the application coordinate downloads, recovery, and post-processing without manually assembling command-line options.

> [!NOTE]
> FluentYTDL is developed for **64-bit Windows 10 and Windows 11**. YouTube has the broadest feature coverage. X support currently targets individual post URLs; profile and timeline downloads are outside the supported scope.

## 🌟 Project impact

FluentYTDL makes advanced yt-dlp and FFmpeg workflows accessible to Windows users who prefer a guided desktop experience. Official release assets have surpassed **20,000 downloads**, while active, issue-driven releases continue to adapt the application to upstream platform and tool changes.

---

## 🪟 Interface preview

| Parse | Format selection | Task queue |
|:--:|:--:|:--:|
| <img src="docs/images/reference/1_quick_start/主程序界面-精确解析.png" alt="Parse page" width="460"> | <img src="docs/images/reference/1_quick_start/精确解析解析页.png" alt="Format selection dialog" width="330"> | <img src="docs/images/reference/1_quick_start/任务列表.png" alt="Task queue" width="460"> |

## ✨ Features

| | |
| --- | --- |
| 🎚️ **Precise format control**<br>Choose resolution, container, codec, audio language, or a simple quality preset. | 📚 **Batch workflows**<br>Parse YouTube playlists and channels, load metadata on demand, and queue selected entries. |
| 🧠 **Resilient tasks**<br>Persist task state, isolate temporary files, configure concurrency, and recover after a restart. | 🛡️ **Quality guard**<br>Detect format downgrades and optionally verify downloaded media with FFprobe. |
| 💬 **Subtitles and covers**<br>Download them independently, merge bilingual subtitles, and embed supported artwork. | ✂️ **Video clipping**<br>Select a time range from an ordinary YouTube video with fast or precise cut modes. |
| 🎞️ **Media processing**<br>Mux and convert with FFmpeg, process SponsorBlock segments, and optionally transform VR projection. | 🔐 **Authenticated sessions**<br>Manage local cookies and use an isolated WebView2 login flow when a site requires a session. |
| 🧰 **Managed components**<br>Update the application and external media tools with checksum verification. | 🪟 **Native desktop experience**<br>Use a focused Fluent interface instead of manually coordinating multiple command-line tools. |

## 🧭 Supported scope

| Area | Current support |
|---|---|
| 🪟 Operating system | Windows 10/11, 64-bit |
| 🌐 Application languages | Simplified Chinese and English; changes apply after restart. Audio/subtitle languages are independent. See [localization and log policy](docs/I18N.md). |
| ▶️ YouTube | Videos, Shorts, live/video URLs, playlists, channels, subtitles, covers, and VR workflows |
| 𝕏 X (Twitter) | Individual post URLs with downloadable media |
| 🌐 Other yt-dlp sites | Not part of the supported product scope |
| 🐍 Source runtime | Python 3.12 or newer |

> [!IMPORTANT]
> FluentYTDL depends on upstream services and tools. Site changes, expired media URLs, account or region restrictions, and yt-dlp regressions can temporarily affect extraction or downloads.

---

## 📦 Installation

### 🪟 Ready-to-use Windows packages

Download the latest installer or portable archive from **[GitHub Releases](https://github.com/SakuraForgot/FluentYTDL/releases/latest)**.

| Package | Best for |
| --- | --- |
| `*-setup.exe` | A standard Windows installation |
| `*-full.7z` | A portable copy with the complete runtime bundle |
| `*-app-core.7z` | Updating only the application core |

> [!TIP]
> Release assets include `SHA256SUMS.txt`. Verify checksums when redistributing or archiving a package. Official binaries are published only through this repository.

### 🛠️ Run from source

```powershell
git clone https://github.com/SakuraForgot/FluentYTDL.git
cd FluentYTDL

# Recommended: uv
uv sync --locked --extra dev --extra build
uv run python main.py
```

Alternatively, use a Python virtual environment and install the project with `pip install -e ".[dev]"`, then run `python main.py`.

> [!NOTE]
> The application manages yt-dlp, FFmpeg, Deno, AtomicParsley, and other external components. A source checkout may download a missing component when its related feature is first used.

---

## 🧑‍💻 Development

| Layer | Implementation |
| --- | --- |
| 🖼️ Desktop UI | PySide6 and PySide6-Fluent-Widgets |
| 🐍 Application core | Python 3.12.12 with a `src/` package layout |
| ⬇️ Download engine | yt-dlp behind a CLI subprocess boundary |
| 🎞️ Media pipeline | FFmpeg and companion media tools |
| 🧱 Service boundaries | Authentication, download, processing, storage, and YouTube services |

```powershell
uv sync --locked --extra dev --extra build
uv run ruff check src tests

$env:QT_QPA_PLATFORM = "offscreen"
uv run pytest tests -q

uv run python scripts/version_manager.py check
```

### 📚 Documentation map

| Read | When you need it |
| --- | --- |
| [Contributing guide](CONTRIBUTING.md) | Setup, workflow, tests, and pull request expectations |
| [Architecture](docs/ARCHITECTURE_EN.md) | Module boundaries and application structure |
| [Development rules](docs/RULES_EN.md) | Repository-specific engineering constraints |
| [yt-dlp integration notes](docs/YTDLP_KNOWLEDGE_EN.md) | Subprocess protocol and downloader behavior |
| [Security policy](SECURITY.md) | Supported versions and private vulnerability reporting |

## 🔐 Security and responsible use

> [!CAUTION]
> Cookies, logs, downloaded media, and local configuration can contain sensitive information. Never post unredacted credentials, cookies, tokens, or private URLs in an issue. Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md).

> [!IMPORTANT]
> Use FluentYTDL only for content you are authorized to access and download. Users are responsible for following applicable law, copyright rules, and platform terms. The project does not grant access to private or restricted content.

## 🤝 Contributing and governance

Bug reports, focused pull requests, documentation improvements, and reproducible platform feedback are welcome.

| | Project document |
| --- | --- |
| 🛠️ | [Contributing guide](CONTRIBUTING.md) |
| 🫶 | [Code of Conduct](CODE_OF_CONDUCT.md) |
| 🧑‍🚀 | [Maintainers](MAINTAINERS.md) |
| 🛡️ | [Security policy](SECURITY.md) |

> [!NOTE]
> Source code is licensed under [GPL-3.0](LICENSE). The [trademark policy](TRADEMARK.md) protects project identity without reducing GPL rights, while the [academic integrity notice](ACADEMIC_HONESTY.md) explains attribution expectations without adding license restrictions.

---

## 💛 Acknowledgements

FluentYTDL stands on the shoulders of an outstanding open-source ecosystem. Special thanks to the projects and people who make its core experience possible:

| | Project | What it brings to FluentYTDL |
| --- | --- | --- |
| 🎬 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Media extraction, format discovery, and download orchestration. |
| 🎞️ | [FFmpeg](https://ffmpeg.org/) | Stream merging, conversion, metadata processing, and VR projection support. |
| 🖥️ | [PySide6](https://doc.qt.io/qtforpython-6/) and [PySide6-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) | The Qt desktop foundation and Fluent-style interface. |
| 🔐 | [rookiepy](https://github.com/thewh1teagle/rookiepy) and [pywebview](https://github.com/r0x0r/pywebview) | Browser-session integration and the isolated WebView2 login experience. |
| 🧩 | [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) and [Deno](https://github.com/denoland/deno) | YouTube player compatibility and JavaScript challenge support. |
| ✨ | [SponsorBlock](https://sponsor.ajay.app/) and [AtomicParsley](https://github.com/wez/atomicparsley) | Sponsor-segment processing and media metadata enrichment. |

### 🌱 Community

Thank you to every upstream maintainer, [code contributor](https://github.com/SakuraForgot/FluentYTDL/graphs/contributors), issue reporter, documentation contributor, and tester. Every reproducible report and thoughtful improvement helps FluentYTDL become more dependable and accessible.

📜 License texts and third-party notices are collected in [`licenses/`](licenses/). If this project helps you, consider supporting the upstream projects that make it possible.
