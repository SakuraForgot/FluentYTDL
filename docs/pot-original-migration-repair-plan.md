# POT 原版组件修复

2026-10-10，本地实现；应用版本仍为 `3.7.7-rc.1`。

## 定稿结构

开发环境使用 `assets/bin`，安装版使用 `bin`：

```text
bin/
├─ deno/deno.exe
├─ yt-dlp/
│  ├─ yt-dlp.exe
│  └─ yt-dlp-plugins/bgutil-ytdlp-pot-provider.zip
└─ pot-provider/
   ├─ manifest.json
   ├─ LICENSE
   └─ server/
      ├─ src/
      ├─ node_modules/
      └─ 上游依赖声明与锁文件
```

官方插件 ZIP 原样保存，遵循 yt-dlp 的标准插件发现路径。POT 与 yt-dlp 复用现有 Deno；自定义 yt-dlp 使用同一 ZIP 的显式插件目录，不再复制插件。取消随机版本目录、hash 插件目录、私有 Deno 和 `_internal/pot-bootstrap`。临时下载及事务备份只用于安装和故障恢复。

## 安装、更新与运行

1. 通过上游 GitHub Release 检查最新版本，下载官方插件 ZIP 并校验 Release 提供的大小与 SHA256。
2. HTTP 服务使用同一标签的上游源码归档；核对服务端版本及三个插件源码与 Release ZIP 一致。ZIP 本身只有 Python 插件，不包含服务端。
3. 使用已有共享 Deno 执行 `deno install --prod --frozen --lock=<临时生产锁文件> --allow-scripts=npm:canvas --node-modules-linker=hoisted`，只在安装、更新或修复时准备依赖。临时锁文件只去除开发专用的顶层 specifier，保留全部锁定版本、依赖关系和 integrity；交付的上游声明及锁文件保持原样。当前便携依赖布局要求 Deno 2.9.7；这不是上游 HTTP 服务本身的最低版本要求。Deno 缺失或不兼容时复用现有组件更新器处理，尊重自定义路径。
4. 候选目录先启动服务，用实际选中的 yt-dlp 请求 Token。随后复用组件更新锁，等待本应用 CLI 结束、停止本应用 POT 进程，再替换固定服务目录、清单和 ZIP。
5. 在最终目录再次请求 Token。成功才报告安装完成；替换或验证失败通过现有 journal 恢复服务端和插件。进程意外退出后启动恢复同一事务。
6. 正常启动只运行本地依赖，`--cached-only --no-remote` 阻止现场补依赖。POT 就绪必须包含实际 yt-dlp Token 请求；只有文件存在、进程运行或 `/ping` 成功均不算就绪。

构建流程同样使用快照中的共享 Deno 和 yt-dlp，记录标准位置的 ZIP；构建可执行离线启动及插件加载检查，其证据与在线 Token 验证区分。

## 依赖瘦身与打包

- Deno 2.9.7 的 hoisted 安装在上游锁文件保留开发入口时，即使指定 `--prod` 或 `--entrypoint` 也会安装开发工具。干净目录复现为 167.55 MiB；仅在临时锁文件移除开发入口后为 87.40 MiB，不再下载 TypeScript、SWC、ESLint、Prettier。
- `core/pot_dependencies.py` 只分析已经落盘的生产依赖树，不自行解析版本。保留共享、可选及 peer 依赖、同名包的嵌套版本；清理非运行文件时保留许可证、运行入口及其数据、Canvas DLL 和 QuickJS WebAssembly 资源。
- 共用顺序为：准备依赖 → 清理 → 生成 POT 清单 → 候选验证 → 事务替换及最终验证。清理发生在临时目录；失败保留旧组件。清理前后字节数、文件数和包数写入安装日志。
- 正式打包在构建目录重建组件，Full/Setup 复制已清理的组件快照。快照校验、复制前后和冻结程序自检复用同一清理检查；旧快照也不能绕过。用户首次启动不执行依赖安装。
- app-core 不携带组件。已有安装通过下一次 POT 更新或修复整体替换，不在启动时直接删除活动目录。
- 组件复制只在 `bin/` 或组件根目录过滤会话目录，保留生产依赖内部的 `cookies` 等同名代码目录。PyInstaller 的 Windows DLL 查找优先使用 System32，避免 PATH 中其他工具的同名 ICU DLL 破坏冻结版 Qt。

当前 2.0.2 的实际依赖体积：

| 指标 | 原始安装 | 精简后 |
| --- | ---: | ---: |
| 文件数 | 9,034 | 3,480 |
| 展开体积 | 167.55 MiB | 66.94 MiB |
| 相同 LZMA2 参数的 7z | 28.30 MiB | 14.13 MiB |

这是 `node_modules` 的对比，不是整个软件安装包的缩减比例。剩余部分包含实际运行所需的 Canvas、DOM、YouTube 和代理依赖。

## 修复覆盖

- 原来追踪 Rust 仓库导致版本停在 0.8.1，现在跟踪 Brainicism 原版 Release。
- 安装不再依赖 FluentYTDL 自建组件 Release，也不使用自建 JSON＋ZIP 导入协议。
- 未安装时直接点击“安装组件”会先检查再下载；失败后按钮可以重试。缺失/旧格式镜像按现有规则回退 GitHub。
- Windows 新复制的 native 依赖目录可能短暂拒绝替换；文件操作增加有上限的重试，持续失败仍回滚。
- 旧嵌套插件目录无法被实际 yt-dlp 默认发现；标准原版 ZIP 已验证可被默认发现。
- 只清理身份与内容可确认的旧组件和插件目录，保留未知用户文件与配置。仅管理本应用拥有的进程。
- 启动失败弹窗提供重试及状态入口。设置页维持与软件更新一致的 70px 横向栏；独立 POT 窗口仅含运行状态、失败原因和重试。

## 验证记录

忽略目录 `build/pot-implementation-20261010/` 保存验证结果：

- `final-layout/acceptance.json`：官方 2.0.2 ZIP，候选和最终固定目录均取得长度 124 的 Token；真实 yt-dlp 无额外插件参数时发现 ZIP；POT 目录没有 Deno。
- `final-ui-install/`、`final-ui-repair/`：设置页安装和修复的 Qt offscreen 验证记录，不代表手动桌面验收。
- `final-reload/result.json`：新进程离线启动检查、实际 Token 就绪、标准目录及共享 Deno 验证通过；组件栏 70px，独立状态窗 520×220。
- `final-regression.log`：537 项相关 Python/Qt 回归通过；后续文件替换和 ZIP 识别的 44 项回归、上游与构建快照的 35 项回归通过（用例有重叠）。

早期 `direct-*` 和自建包记录属于已撤销设计的历史诊断，不作为最终布局的验收依据。Token 就绪不等于任意视频解析或下载已通过；本轮未创建公开 Release、推送代码或部署 ControlCenter。

依赖与打包修复的记录在 `build/pot-prune-20261010/`：

- `development-repair.log`：开发环境通过现有修复流程替换组件，使用共享 Deno 2.9.7，实际 Token 长度为 124。
- `development-acceptance.json`：官方 ZIP 和上游声明、锁文件保持原样；离线冷启动、依赖清洁度、压缩前后体积校验通过。
- `proxy-acceptance.json`：HTTP 与 SOCKS5 模式均取得 Token。关闭显式代理时未取得 Token，未精简对照组同样失败，不能据此认定是精简导致；也不将此记录视为直连验收通过。
- `regression-final.log`：193 项相关回归通过，包含误删依赖内部 `cookies` 目录的防回归用例。
- Full/Setup 必须来自同次构建快照并通过冻结程序自检；本机验证 Setup 时只解包校验载荷，不运行会改动注册表、快捷方式及卸载状态的安装生命周期测试。
