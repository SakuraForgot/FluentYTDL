# 新版架构 V2 · VS-19 安装、便携、覆盖安装与卸载

证据：2026-09-19 source/static；未启动安装器/维护脚本、未读真实用户配置或执行删除。CONFIRMED 为代码，INFERRED 为含义/风险，UNKNOWN 为未证，RECOMMENDATION 为受控验收。

## 1. 安装与便携入口不同

[CONFIRMED] Full 包在归档时注入 portable.txt；setup 共用干净应用树但不带该标记：[build.create_7z，L708](../../../scripts/build.py#L708)。`user_data_dir` 优先 override，其次 frozen portable 的 exe 目录、installed LocalAppData，源码开发则项目根：[paths，L81](../../../src/fluentytdl/utils/paths.py#L81)。认证目录还依赖运行 bin/dle_user，不能概括所有状态都在数据根。

[CONFIRMED] Inno AppId 保持固定，默认 PrivilegesRequired=lowest、允许安装作用域选择、复用既有 scope，声明 x64compatible：[iss，L46](../../../installer/FluentYTDL.iss#L46)、[L84](../../../installer/FluentYTDL.iss#L84)、[L104](../../../installer/FluentYTDL.iss#L104)。这是安装器准入配置，不等同于 ARM/所有 Windows 版本已实测。

## 2. 正常调用链与资源变更

| 用户动作 | [CONFIRMED] 调用链 | 写入、退出和缘由 |
|---|---|---|
| 新安装/覆盖安装 | [PrepareToInstall，L173](../../../installer/FluentYTDL.iss#L173) 检查另一作用域同 AppId，提取 helper，Maintain Stop；Inno 拷贝载荷 | Stop 不清数据；另一 scope 冲突拒绝，避免同一产品两套权限登记混用。 |
| 安装后登记 | [CurStepChanged，L190](../../../installer/FluentYTDL.iss#L190) Register，首个 install-language.txt，可选 AddPath | owner record、首次语言与 PATH 记录；不覆盖既有语言文件。 |
| Stop | [maintenance，L35–63](../../../installer/maintenance.ps1#L35) 路径匹配主 EXE，计算子进程闭包，CloseMainWindow，5 秒后强停仍同 CreationDate 的 PID | 检测本 appRoot 的 updater.exe 正运行则失败；不是全局按 exe 名杀进程。 |
| AddPath/RemovePath | [L66–103](../../../installer/maintenance.ps1#L66) user 用 owner SID 的用户 hive，machine 用 HKLM；记录此次实际新增条目 | 卸载仅删除登记过的 bin 子目录，保留原有 PATH 项和值类型；原用户 hive 不可用时报错。 |
| 卸载 | [CurUninstallStepChanged，L208–220](../../../installer/FluentYTDL.iss#L208) Stop → Clean → RemovePath，成功后删 install-language/owner | 清理异常中断并显示维护失败；不会无条件宣称数据已清。 |

[INFERRED] 安装与卸载调用不同动作，避免升级为获取新 EXE 而先删认证/历史；owner SID 与 path ownership 支撑跨提权但同原用户的维护。代价是原用户 profile/hive 缺失会阻断安全识别，需要可解释错误而不是猜用户目录。

## 3. Clean 如何发现与删除

[CONFIRMED] 脚本先拒绝卷根 AppDirectory 和任一祖先 reparse point：[参数/Assert-NoReparse，L1–23](../../../installer/maintenance.ps1#L1)。machine 枚举非 Special 的真实 Windows profile；user 从 owner SID 唯一解析 profile。候选包括 appRoot、Local/Roaming/Documents/重定向 Known Folders、匹配当前 app_dir 的 installation records 和 owner marker 验证过的 override data_dir：[L107–173](../../../installer/maintenance.ps1#L107)。

[CONFIRMED] 删除配置前读取 download_dir/quick_download_dir 作为保护集合；配置解析失败记 failure，但继续其它项；`Remove-OwnedTree` 不删除与下载目标完全相等的根、递归时再次拒绝 reparse、保留指定媒体后缀、只在目录空时删目录：[L176–204](../../../installer/maintenance.ps1#L176)。范围是已知应用条目，不是整个 appRoot 或整个用户 Documents 递归删除。

[CONFIRMED] 已知清理条目包括 config/tasks DB/WAL/state/log/cache/data/临时目录/更新残余等；认证根和 Cookie 以 PreserveMedia=false 删除；每个用户的临时认证 cache 也清理。**清理成功后才删 data owner marker 与 registration**，使失败后还可重新发现 override 根：[L205–240](../../../installer/maintenance.ps1#L205)。

[INFERRED] 记录保留至最后，是为可重试恢复；下载目标和后缀保护降低误删用户媒体的概率，代价是不能仅按“目录已知”保证所有文件语义，也不能把扩展名保护宣传为识别全部媒体类型。未知顶层项不在列表时不删除；列表内应用拥有目录中的非媒体项仍会递归清理。

## 4. 已发现的行为边界

| 事实/风险 | 代码证据 | 对接受标准的影响 |
|---|---|---|
| [CONFIRMED] 公告 SQLite 没列入清理 | [AnnouncementService，L159](../../../src/fluentytdl/notification/announcement_service.py#L159) 位置为根 announcements.sqlite3；[Clean 列表，L205](../../../installer/maintenance.ps1#L205) 无此文件 | [INFERRED] 已通知/已确认/快照可能残留；未真实卸载，记静态清单差异，不宣称全部隐私材料已清。 |
| [CONFIRMED] Stop 在 5 秒后仍可强停主/子进程 | [Stop，L55–60](../../../installer/maintenance.ps1#L55) | [UNKNOWN] 正在 Staging commit 时断点补偿/文件结果未验；“请求窗口关闭”不等于已完成事务排空。 |
| [CONFIRMED] 下载保护基于所读取配置和后缀集 | [L176–202](../../../installer/maintenance.ps1#L176) | [UNKNOWN] 损坏配置、未登记自定义根、额外图片/附件扩展名的保护范围不能自动扩张。 |
| [CONFIRMED] owner discovery/清理失败积累后返回 1 | [L223–240](../../../installer/maintenance.ps1#L223) | 可能已删一部分且保留另一部分；不是全有或全无事务，必须报告局部结果。 |

## 5. 后续验收

[RECOMMENDATION] 临时隔离用户/目录验证 current-user 和 machine，首次安装/覆盖安装保留账户，卸载清理已知数据但保留媒体与未知顶层文件，覆盖 reparse、下载根等于数据根、配置损坏、owner 缺失、offline hive、override registration、公告 DB 和清理中权限失败。验证失败后 discovery 记录仍能支持重试。只读文档阶段不执行这套破坏性操作。

[UNKNOWN] 本轮不证明现有测试已经覆盖这些交错；[scripts/test_installer.py](../../../scripts/test_installer.py)、[test_paths_migration.py](../../../tests/test_paths_migration.py) 只是后续入口。发布包是否含正确维护 helper 必须从最终 app-core/setup 文件检查，而非仅看 .iss Source 声明。
