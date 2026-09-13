# pre 更新通道实现与验收（2026-09-12）

## 行为

- 所有安装版本默认 stable，配置键为 `app_update_channel`；选择 pre 必须确认风险。
- pre 为用户通道名，发布版本继续采用 `X.Y.Z-rc.N`。beta 保持仅构建产物分发。
- 支持 rc 序号升级、rc 升级到同版本正式版；禁止降级。切回 stable 等待更新的正式版。
- 检查/下载/安装期间不接受通道变更；实际切换清空旧清单并重新检查。
- 跳过版本按通道隔离，只抑制静默检查。手动检查允许重新显示该版本。
- GitHub 与 Cloudflare 使用相同候选选择规则；旧缓存服务不标识 pre 时拒绝并按既有规则换源。

## 本任务文件清单

FluentYTDL：

- `src/fluentytdl/utils/app_version.py`：完整应用版本排序与公开候选格式。
- `src/fluentytdl/core/config_manager.py`：通道与跳过版本默认配置。
- `src/fluentytdl/core/component_update_manager.py`：通道状态、清单请求、完整版本比较。
- `src/fluentytdl/core/update_transport.py`：pre Release 查询与缓存响应验证。
- `src/fluentytdl/ui/components/settings/app_update_card.py`：通道选择、风险确认及检查状态。
- `src/fluentytdl/ui/components/dialogs/update_dialog.py`：通道隔离的跳过版本。
- `scripts/release_pipeline.py`、`scripts/sync_control_center.py`：发布说明与双通道刷新校验。
- `tests/test_app_update_channels.py`、`tests/test_component_update_manager.py`、`tests/test_release_protocol.py`：回归覆盖。
- `assets/locales/fluentytdl_en_US.ts`、`fluentytdl_zh_CN.ts`：仅新增五条通道文案；编译对应地区和语言别名 QM。
- `docs/build_release.md`、本文：发布顺序、功能边界与验收记录。

同级 FluentYTDL-ControlCenter：

- `apps/worker/src/updates.ts`：pre 独立同步、排序与清单身份校验。
- `apps/worker/src/index.ts`：按 channel 读取独立 KV 缓存。
- `tests/worker.test.ts`：双通道缓存、同步数量和版本选择回归。
- `dist/`：由 `npm run check` 重新构建。

## 验证结果及边界

- Python：更新通道、协调器、传输、发布协议/流水线、更新器、翻译完整性，共 293 项通过。
- 修改的 Python 逻辑及测试通过 Ruff 检查与格式化。
- ControlCenter `npm run check`：类型检查、12 项测试、管理界面和 Worker 构建通过。
- 未运行全仓 Python 测试，未构建新发布 EXE，未做真实安装升级/回滚验收。
- 未部署 Worker、执行远端同步、创建 tag 或发布 Release。上线顺序：部署 ControlCenter → 刷新并核验两个缓存 → 按既有流程发布新客户端。
- 仓库原有未提交改动保留；本任务没有提交或暂存文件。翻译文件既有段落仍存在 diff whitespace 提示，未改动这些段落。
