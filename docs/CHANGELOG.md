# 更新记录

## v3.7.2-rc.1

本版为打包、安装器与更新兼容性的预发布验证版，不替代稳定版 Latest。rc 客户端保持手动下载更新，可从 GitHub Releases 获取后续版本。

- 统一 Windows x64、Python 3.12.12 构建环境，使用独立构建目录和严格锁文件检查。
- 每次发布获取最新 yt-dlp、FFmpeg/ffprobe、Deno、AtomicParsley、POT Provider 及内嵌 7-Zip，并记录校验和及组件来源。
- 修复更新归档解压兼容性，保留独立 updater 的失败回滚和自更新机制。
- 补齐 Python.NET/WebView2 打包依赖及启动诊断，改善登录组件加载失败的错误信息。
- 安装器支持简体中文和英文，默认当前用户安装，可选所有用户；PATH 默认不勾选。
- 明确卸载清理账号、Cookie、配置和历史记录，保留下载成品；覆盖安装、应用更新和回滚保留运行数据。
- 加入冻结启动、真实归档、安装生命周期和发布资产的自动校验。

重点验证：两种安装范围、中英文安装、覆盖安装、WebView2 登录、更新失败回退与卸载数据边界。尚未完成的实机项目见 `packaging_implementation_record.md`。
