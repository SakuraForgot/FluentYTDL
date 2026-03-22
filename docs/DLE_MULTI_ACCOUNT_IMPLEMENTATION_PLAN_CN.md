# FluentYTDL DLE 真多账号存储与独立登录控件实施计划

## 1. 目标

将当前单账号 DLE 登录流程升级为真多账号方案，满足以下核心能力：

1. 支持多个 YouTube 账号独立存储，不互相覆盖。
2. 用户可在 UI 中明确选择账号（如 A/B/C）作为当前下载使用账号。
3. 每个账号绑定独立 WebView2 登录容器（profile 隔离）。
4. 登录流程支持“先切换/退出，再手动确认提取”，避免现有“秒提取”问题。
5. 保持下载链路兼容：下载层仍读取统一 cookiefile，不强制重构下载模块。

## 2. 非目标

1. 本期不实现跨平台统一（优先 Windows + WebView2）。
2. 本期不做云端账号同步。
3. 本期不引入 OAuth 网页授权流程（仍基于 Cookie 提取）。

## 3. 现状问题

1. DLE WebView2 使用固定持久化目录，导致登录态复用，难以切换账号。
2. DLE Cookie 缓存文件固定为单文件，无法并存多个账号。
3. UI 仅有“登录并提取”按钮，无“账号选择 + 会话管理 + 手动确认提取”。
4. 在已登录状态下会快速提取，用户没有充足时间退出旧账号。

## 4. 目标架构

### 4.1 核心思路

1. 账号维度隔离存储：每个账号独立 profile_dir 和 cached_cookie_path。
2. 激活账号机制：仅“当前激活账号”会被同步到统一 bin/cookies.txt。
3. 双阶段登录提取：
   1. 阶段 A：打开账号专属 Web 控件，允许用户登录/退出/切换。
   2. 阶段 B：用户点击“我已完成，开始提取”后，才执行 Cookie 提取。

### 4.2 新增组件

1. DLEAccountStore：管理账号元数据与状态。
2. DLESessionController：管理 Web 控件生命周期（打开、关闭、确认提取）。
3. DLEAccountSelectorWidget：账号列表、当前账号切换、登录/登出操作入口。

## 5. 数据模型设计

### 5.1 账号模型 DLEAccount

建议字段：

1. account_id: str（UUID）
2. display_name: str（用户可编辑，如 A 账号）
3. platform: str（固定 youtube，预留扩展）
4. profile_dir: str（独立 WebView2 容器目录）
5. cached_cookie_path: str（独立 Cookie 缓存）
6. last_extracted_at: str | null
7. cookie_count: int
8. valid: bool
9. is_default: bool
10. notes: str | null（可选）

### 5.2 建议存储文件

1. auth_service.cache_dir/dle_accounts.json
2. auth_service.cache_dir/dle_accounts/{account_id}/profile（WebView2 存储）
3. auth_service.cache_dir/dle_accounts/{account_id}/cached_dle_youtube.txt

### 5.3 与现有配置关系

1. 继续保留 auth_config.json 的 source 信息。
2. 新增 current_dle_account_id 字段，标记当前激活账号。
3. 旧 cached_dle_youtube.txt 在迁移期映射到 default 账号。

## 6. UI 设计方案

## 6.1 设置页新增分区：DLE 多账号

建议新增卡片：

1. 当前账号选择卡
2. 账号管理卡（新增、重命名、删除）
3. 登录会话卡（打开登录窗口、确认提取、关闭窗口）
4. 当前账号状态卡（Cookie 有效性、提取时间、条数）

## 6.2 下载失败弹窗中的最小增强

在下载配置窗口的 DLE 面板中增加：

1. 账号下拉框（仅显示已创建账号）
2. 按钮：打开登录窗口
3. 按钮：确认提取并重试
4. 状态文案：当前账号、会话是否打开、最后提取状态

## 6.3 交互细节

1. 用户切换账号时，不自动提取。
2. 用户点击“打开登录窗口”时，加载该账号 profile_dir。
3. 用户点击“确认提取”后才执行提取并写入该账号 cached_cookie_path。
4. 提取成功后自动将该账号设为当前激活账号，并同步到 bin/cookies.txt。

## 7. 流程设计

## 7.1 首次迁移流程

1. 启动时检测是否存在旧 cached_dle_youtube.txt。
2. 若存在且无新账号数据：创建 default 账号并导入旧缓存。
3. 设置 current_dle_account_id = default。

## 7.2 登录提取流程（新）

1. 用户选择账号 A。
2. 点击“打开登录窗口”：使用 A.profile_dir 打开独立 WebView2。
3. 用户在窗口中自行退出旧账号并登录 A。
4. 用户回到主程序点击“确认提取”。
5. 程序从 A.profile_dir 提取 Cookie，写入 A.cached_cookie_path。
6. 验证通过后复制到 bin/cookies.txt，并更新 meta source 为 dle:A。

## 7.3 下载使用流程

1. 下载前读取 current_dle_account_id。
2. 使用该账号 cached_cookie_path 作为来源。
3. CookieSentinel 仍统一维护 bin/cookies.txt，下载层保持不变。

## 8. 模块改造清单

## 8.1 auth_service.py

1. 新增 DLE 账号 CRUD API：
   1. list_dle_accounts
   2. create_dle_account
   3. update_dle_account
   4. delete_dle_account
   5. set_current_dle_account
2. DLE 分支支持按 account_id 读取/写入缓存路径。
3. 保留旧接口，新增可选参数 account_id。

## 8.2 webview2_provider.py

1. extract_cookies 支持传入 profile_dir。
2. 提供两阶段 API：
   1. open_session(profile_dir)
   2. extract_from_open_session(session_id)
3. 增加 close_session(session_id)。

## 8.3 cookie_sentinel.py

1. meta 增加 account_id 字段。
2. get_status_info 返回当前激活账号信息。
3. 同步逻辑改为“按当前激活账号复制”。

## 8.4 settings_page.py

1. 新增 DLE 多账号 UI 组件挂载。
2. 重构原 _on_dle_login_clicked，拆分为：
   1. 打开登录窗口
   2. 确认提取
3. 增加账号切换事件处理。

## 8.5 download_config_window.py

1. DLE 重试面板增加账号选择。
2. 重试逻辑改为“确认提取并重试解析”。

## 9. 分阶段实施计划

## 阶段 1：数据层与兼容迁移（1-2 天）

1. 新增 dle_accounts.json 与迁移逻辑。
2. 完成 auth_service 账号 CRUD 与 current_dle_account_id。
3. 保持旧 UI 可运行。

交付标准：

1. 可创建多个账号并持久化。
2. 旧缓存可自动迁移为 default 账号。

## 阶段 2：提取链路账号化（2-3 天）

1. WebView2 provider 支持 profile_dir 注入。
2. DLE 缓存路径按 account_id 分离。
3. CookieSentinel 支持按激活账号同步。

交付标准：

1. A/B 两账号可分别提取并各自保留 Cookie。
2. 切换当前账号后下载使用对应 Cookie。

## 阶段 3：UI 与双阶段交互（2-3 天）

1. 设置页加入账号管理与会话控制。
2. 下载配置窗口加入账号选择和确认提取入口。
3. 状态提示与异常处理完善。

交付标准：

1. 用户能明确执行“打开登录窗口 -> 手动切号 -> 确认提取”。
2. 不再出现“刚打开就自动提取导致没时间退出”的体验问题。

## 阶段 4：测试与稳定性（1-2 天）

1. 单元测试：账号 CRUD、迁移、路径隔离。
2. 集成测试：A/B 账号切换提取与下载注入。
3. 手动测试：窗口关闭、超时、提取失败回退。

交付标准：

1. 关键用例全部通过。
2. 失败路径文案清晰，且不会覆盖其他账号缓存。

## 10. 测试清单

1. 创建账号 A、B，分别登录提取后验证 cookie_count 不同且文件路径不同。
2. 切换当前账号 A 下载成功，再切 B 下载成功。
3. 删除账号 A 后不影响 B。
4. 未确认提取前关闭登录窗口，不应覆盖当前有效 Cookie。
5. 提取失败时保留旧可用 Cookie，且提示明确。

## 11. 风险与缓解

1. 风险：WebView2 会话控制接口与现有实现耦合高。
   1. 缓解：先以“先打开后再独立触发提取”的弱耦合方案落地，再演进成长会话对象。
2. 风险：多账号 UI 增加复杂度。
   1. 缓解：先做基础列表 + 当前账号切换，不一次性上复杂权限逻辑。
3. 风险：旧版本配置兼容。
   1. 缓解：提供一次性迁移脚本与回滚开关。

## 12. 验收标准

1. 至少支持 3 个 DLE 账号独立存储并可切换。
2. 账号切换后，下载实际使用对应账号 Cookie。
3. 登录流程支持用户手动控制提取时机。
4. 现有浏览器提取模式与手动导入模式不回归。

## 13. 建议后续增强

1. 账号标签（如 主号/备用/会员）。
2. 账号导出/导入（仅导出元信息，不导出敏感 Cookie）。
3. 账号健康检查任务（定时检测过期）。
4. 下载任务维度绑定账号（高级模式）。
