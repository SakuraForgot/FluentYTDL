# 打包改造实施与验收记录

本记录对应 `build_release.md`，版本保持 3.7.1。未打 tag、未公开发布、未改写现有 Release。

## 已落地

- Python 3.12.12 / Windows x64 单一环境，集中工具版本，locked 同步，共用预检；版本覆盖只写入独立构建目录。
- 每次非 spec 构建解析全部最新组件，包括官方 7-Zip；校验上游摘要、文件集合与落盘哈希，记录版本漂移，显式快照重现不可发布。
- 独立构建目录、冻结启动自检、两类归档与冻结 updater 实解压验收；成功产物清单驱动交付、校验和及发布。
- 中英文 Inno、安装范围、首次语言种子、按安装实例停止进程、PATH 归属记录、按实际用户清理运行数据；更新包携带卸载维护脚本。
- 共用检查、稳定 Required checks、Draft 下载核验后自动公开、公开下载及 stable latest 校验、公开版本禁止覆盖。
- 规则与翻译定向同步检查；清理历史格式差异并启用 Ruff 格式门禁，补齐英文目录中 26 条空译文。

## 本地已有验收证据

- 最终全量 `pytest tests` 已通过：1735 passed、2 skipped（50.69 秒）。两项跳过分别是本机缺少符号链接权限，以及已有下载模块尚未实现的 Step 6 测试；不是发布依赖缺失形成的跳过。报告：`build/acceptance-tests.xml`。
- 新增发布与清理测试覆盖单次解析、摘要失败、缺失资产、快照越界、只读版本覆盖、产物篡改、禁止快照发布、禁止覆盖公开版本、双语消息、目录联接拒绝和临时模拟双用户的真实 PowerShell 清理逻辑。
- `uv lock --check`、版本一致性、Ruff 检查与格式检查、actionlint 已通过。
- Pyright 在显式指定项目 Python 后报告 270 errors、10 warnings，按方案保持 advisory，未宣称类型检查通过。报告：`build/pyright-advisory.log`。
- Inno 6.7.1 实际编译通过。编译器通过解包取得，没有在开发机执行安装器生命周期测试。
- 维护脚本输出通过 Inno [ExecAndLogOutput](https://jrsoftware.org/ishelp/topic_isxfunc_execandlogoutput.htm) 纳入安装/卸载日志，记录实际失败路径；清理失败时保留自定义数据根的发现记录以便重试。
- 完整 all 构建已产出 Full、app-core、Setup、更新清单和校验和。Full/app-core 的归档编码、文件哈希及冻结 updater 在空 PATH、中文路径中的真实解压通过；冻结 Qt/Python.NET 自检通过。
- 最终构建的机器可读证据位于 `build/latest-result.json` 指向的本次目录；各次历史产物保留在 `build/runs/`，不能把历史目录整体作为发布集合。

最终成功演练为 `build/runs/5af566c3e46444cf8b5697c2272a4559/`：`target=all`、`component_policy=replay`。它显式重现本轮从上游最新取得的 `6de1598e816446da98e0a7e3d9e61ad7/components/snapshot.json`，用于本地诊断；正式发布门禁会拒绝该产物集合，正式发布仍必须重新获取所有最新组件。一次终端中断后的未完成构建没有被提升为成功结果。

- 原生 7-Zip 后端 Full/app-core 验证报告：最终构建目录的 `archive-reports/`。
- py7zr 后端真实 Full/app-core 验证：`build/runs/9547fef7c8fd48988b1516752d40dde6/py7zr-verification/result.json`；分别验证 543、535 个文件，冻结 updater 实解压通过。这是独立诊断产物，不混入最终交付集合。
- Setup 解包后 542 个文件与最终冻结目录逐文件哈希一致，解出的主程序自检通过：`build/delivery-verification/8bd87b05b3f94d328f53db88dd51db88/result.json`。没有在开发机执行安装/卸载。
- 最终五项产物已通过共用发布校验并暂存到 `build/upload/`，没有上传至远端。
- `tests/test_release_protocol.py` 的 12 项本地协议/故障注入测试覆盖 stable、rc、beta、错误版本与 tag、main 历史约束、部分产物禁发、Draft 恢复与缺失资产、公开资产损坏及 latest 内容不一致；模拟测试不能替代真实 GitHub 服务验收。

最终构建完成后仅补入协议测试及本验收记录，应用、安装器与构建/发布实现没有再变更。覆盖安装已设为重写卸载日志，避免继承旧版全局终止进程及递归清理用户目录的指令；新安装器不再管理的旧文件可能保留，详见 `build_release.md`。

## 构建目录与失败回滚的专项确认

对比线上 main / v3.7.1 提交 `2f90b6e4ff9306c716144a6a68321cbe051cb885`，updater 原有函数中只有解压实现发生变化；备份、失败判定、恢复旧程序及自更新提交逻辑未改。独立构建目录不进入安装后的路径协议，暂存 VERSION 仍打包为 `_internal/VERSION`，随整个 `_internal` 备份和恢复。

线上 updater 源码与工作区 updater 源码分别读取真实新 app-core 包、模拟启动失败，均成功恢复旧 EXE 和运行时版本号，保留用户数据并删除失败批次的 `updater.exe.new`。临时目录验证报告为 `build/online-rollback-audit.json`；进程启动已模拟，不涉及真实安装、账号切换或提权。

`tests/test_updater.py` 持续验证 ready/survival 两种监护模式、正式/rc 两种新版本的失败恢复：检查 `_internal/VERSION` 优先于遗留根 VERSION，旧程序和维护脚本恢复，配置、数据库及 WAL/SHM、Cookie、账号、迁移标记、安装归属、下载文件保持不变，且旧程序启动不携带新版专属参数。

已确认的存量缺口：根目录 `BUILD_INFO.json` 不随回滚恢复，可能保留失败版本的构建溯源信息。线上与本地均存在；当前应用版本判定不读取它，所以不影响上述运行时版本恢复。本次专项仅确认并记录，没有扩大为更新事务重构。

## 尚需外部环境验收

这些项目没有在当前开发机上完成，不视为通过：

| 项目 | 已准备的入口或条件 |
| --- | --- |
| 临时 Windows runner 上四种语言/范围组合的新装、覆盖安装、卸载 | release.yml 调用 scripts/test_installer.py；要求 GitHub-hosted 隔离 runner，拒绝开发机 |
| 中英文 100%、150%、200% DPI 布局 | 实机逐页截图及人工记录 |
| 原使用者跨账号 UAC、多个真实用户、离线用户配置文件与重定向目录 | 隔离 Windows 账号环境；无法确认归属或清理失败必须失败并留下残留报告 |
| 真实 WebView2 登录 | 有 WebView2 runtime 的交互环境；保留已有诊断修复 |
| 旧 updater 连续升级两次、自替换及启动失败回滚 | 旧版本与新版本实机演练；现有归档/更新单测不能替代 |
| Draft/公开下载服务行为和故障注入 | 测试仓库；本轮未向生产仓库发布资产 |
| main 分支保护 | 新工作流合入且 main 上 Required checks 成功后，运行 configure_branch_protection.py --apply |

分支保护脚本在当前 main 尚无新检查成功记录时明确拒绝修改。PATH 的原用户 hive 不可用时同样明确失败，不改写提权账号的 PATH。当前提供了相应机制和测试入口，但这些外部验收仍需执行后才能宣称完整生命周期验收完成。
