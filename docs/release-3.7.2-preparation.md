# 3.7.2 稳定版发布准备（2026-09-13）

## 当前范围与版本

- 本文件替代 `release-3.7.2-rc.2-preparation.md` 中的下一版本发布计划；旧文件保留为历史记录。
- 当前工作分支仍为 `release/3.7.2-rc.2`，HEAD 为 `129b787`。在此基础上保留并整合已有 Cookie 模式、运行时统一及通知操作等未提交改动。
- 已通过 `scripts/version_manager.py set 3.7.2` 同步 VERSION、pyproject.toml、Inno 默认版本和 uv.lock；锁文件只改变本项目版本，未升级依赖。
- 目标标签为 `v3.7.2`，发布属性为 stable、prerelease=false、latest=true。
- 查询时远端 main 为 `6fceb95f7d069a074b61bc0ef186a9a6ebf9687d`，稳定版 Latest 为 3.7.1，预发布为 3.7.2-rc.1，尚无 v3.7.2 标签。执行推送前须刷新远端状态。
- 面向用户的说明位于 `docs/CHANGELOG.md` 的 v3.7.2 节；生成清单与 Release 正文沿用现有读取链路，已验证提取正确版本段落。

## 发布内容

整合 3.7.1 之后的本地化、日志及诊断、更新元数据与公告、stable/pre 通道、安装器和打包改进，以及请求级 YouTube Cookie 开关、实际 yt-dlp 路径与版本统一、POT 首次解析就绪要求、通知中心清空和布局调整。具体用户说明以 CHANGELOG 为准。

Issue 核对不等同于关闭结论：#85 频道映射和 #88 点名译文已有源码修复；#96 需小屏幕及 DPI 验收；#98 需真实登录验收；#91 仍缺原视频及合并后音视频轨道完整性证据。#90/#97/#95/#94/#92 不能因具备恢复机制就宣称全部解析问题消失。#89 TikTok 等平台尚未实现。本次不自动关闭这些 Issue。

## 本地验证

- Python 3.12.12；`uv sync --locked --extra dev --extra build` 通过。
- `uv lock --check`、版本一致性检查通过。
- Ruff check 通过；319 个 Python 文件格式检查通过。
- 规则重新生成；翻译提取、审计和编译通过：3349 个标记调用、25 个已审阅例外，每种语言 2897 个已完成译文，无未完成条目。
- `git diff --check` 通过（Git 提示 Windows 换行转换，不是 whitespace 错误）。
- 全套 `.venv/Scripts/python.exe -m pytest tests -q --tb=short --junitxml=build/pre-3.7.2-tests.xml`：1883 passed、2 skipped、96 warnings，236.97 秒，退出码 0。该结果使用现有本地环境，未开启 CI 的强制最新组件门禁；不能替代正式构建验证。
- Pyright 显式指定 `.venv/Scripts/python.exe` 后仍为 423 errors、10 warnings，退出码 1，未通过；项目 CI 将其设为 advisory，本次没有扩大范围清理类型问题。结果位于 `build/pre-3.7.2-pyright.log`。最初 uv 入口报 trampoline 路径错误，直接模块运行后又发现默认解释器依赖解析错误，因此最终以显式解释器结果为准。

## 推送与发布顺序

1. 复查最终 diff，将本次完整集成改动提交；建议提交标题为 `release: prepare 3.7.2 stable release`。当前尚未暂存、提交或推送。
2. 刷新远端，推送准备分支并以 PR 面向 main 执行 CI；分支本身不匹配仅监听 main/dev 的 push 检查。发布提交必须进入 main，且 Required checks 成功。
3. 可先用 release.yml 手动执行 targets=all、publish=false 验证完整构建。每次正式构建重新获取全部最新附加组件；历史快照不可用于公开发布。
4. 核对 ControlCenter stable/pre 接口和同步工作流配置。配套服务需独立检查，本次未部署或修改其设置。
5. 在通过验证的 main 提交上创建并推送 v3.7.2；标签工作流将重新执行共享检查、最新组件完整构建、隔离 runner 安装生命周期、资产核验，然后公开同一批文件为 Latest。
6. 核验 GitHub 公共下载、latest 更新清单及 Cloudflare 稳定通道元数据一致性。

## 尚未完成的发布验收

- 新版本全量构建、冻结 EXE、安装器生命周期及公开资产下载校验尚未执行，不能把本地 pytest 当作这些验收通过。
- 真实 WebView2 登录、Cookie 开关完整解析/下载交互、受限视频匿名失败行为、小屏幕与 100/150/200% DPI、旧版本连续升级和真实回滚仍需实机证据。
- 不在开发者机器执行隔离 runner 专用卸载验收脚本。
- 本次只准备源码和发布说明，没有创建 tag、公开 Release 或改变线上 Latest。
