# 3.7.2-rc.2 提交准备（2026-09-12）

## Git 基线与版本

- 已 fetch origin；准备基线为 `6fceb95`，与 `origin/main`、`origin/release/3.7.2-rc.1`、`v3.7.2-rc.1` 一致。
- 原本地 `main` 在 `2f90b6e`，落后远端 4 个提交；保留既有历史分支。
- 本次工作分支：`release/3.7.2-rc.2`，从上述基线创建并保留全部工作区改动。
- 使用版本管理脚本将 VERSION、pyproject.toml、Inno 默认版本及 uv.lock 同步为 `3.7.2-rc.2`。
- 继续走 pre：Git 标签采用 `v3.7.2-rc.2`，公开发布属性为 prerelease=true、latest=false。没有创建标签或发布。

## 本次提交范围

| 功能组 | 内容与边界 |
| --- | --- |
| 中英本地化 | 统一界面、服务消息和日志语言；保留简中及英文 TS/QM、语言别名和运行词表；删除日语及繁中应用目录，音轨和字幕语言选择不受影响。包含抽取、审计、编译脚本与对应文档。 |
| 日志与诊断 | 会话日志、脱敏、保留配额、历史分页、后台读取、诊断包、任务摘要及警告治理；包含根 conftest.py 的测试数据隔离。 |
| 更新与公告 | GitHub/Cloudflare 元数据传输、pre/stable 通道、公告显示、更新后重启与同步工作流；二进制继续由 GitHub 下载。 |
| 下载与认证配套 | POT 默认启用、依赖管理以及解析、下载、认证模块中的消息和诊断调整；以实际 diff 与测试为准。 |
| 图标预览 | 流光 SVG、HTML 和参考 PNG；只是独立预览，未接入桌面动态图标。 |
| 提交整理 | 删除重复 skipped_pre_version 默认键，修正三个 Python 文件的格式，更新测试隔离规则及生成的规则副本；忽略本地 artifacts/，原文件保留。 |

这些功能共同修改配置、日志、界面与翻译文件，建议作为一次经过完整验证的版本集成提交，避免按文件硬拆出无法运行的中间状态。

建议提交标题：`release: prepare 3.7.2-rc.2 prerelease`

## 验证与提交边界

- `uv sync --locked --extra dev --extra build`、`uv lock --check`、版本一致性检查通过。
- 全仓 Ruff 通过；315 个 Python 文件格式检查通过。
- 翻译抽取、审计和编译通过：3335 个标记调用、25 个已审阅例外；每种语言 2884 个已完成条目。
- 已同步生成规则文件。
- 除 TS 翻译目录外，`git diff --check` 通过。TS 中多行源文案和翻译含有行尾空格，保留字符串内容以避免改变翻译匹配；完整 whitespace 检查仍会报告这些行。
- 最终使用 `.venv/Scripts/python.exe -m pytest tests -q --tb=short --junitxml=build/pre-rc2-tests.xml` 复核：1863 passed、2 skipped、98 warnings，57.61 秒，退出码 0。首次通过 uv 运行也完成全部测试，但工具返回退出码 1，原因未确认；没有把首次结果视作门禁通过。报告保存在本地 `build/pre-rc2-tests.xml` 和 `build/pre-rc2-pytest.log`，不纳入源码提交。
- 未构建新 EXE、未执行完整最新组件发布门禁、真实安装/升级/回滚或网站下载验收。

## 配套仓库与发布前提

- 同级 `FluentYTDL-ControlCenter` 当前没有 Git 仓库，其 Worker/后台源文件不会随本客户端提交保存。上线前应单独管理其源码版本，按 `docs/pre_update_acceptance.md` 先部署通道接口，再刷新并核验两个缓存。
- 同级参考仓库有一份未跟踪说明文件，不属于本次提交；两个 wiki 目录未纳入本次客户端准备。
- 新同步工作流使用 `FLUENTYTDL_CONTROL_CENTER_SYNC_TOKEN`；本次未读取或修改远端秘密、未部署或执行同步。
- 发布前需将改动合入 main 并通过 Required checks；随后以新标签触发完整预发布流程。准备分支本身不会触发仅监听 main/dev push 的 CI，可通过面向 main 的 PR 执行。
- 本次仅准备工作区和提交说明，未暂存、commit、push、创建 PR 或 tag。
