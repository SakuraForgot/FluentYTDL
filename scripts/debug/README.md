# 错误诊断 —— 故障注入与断点验证手册

> 配套工具：同目录的 `inject_error.py`。**长期保留，供后续回归复用。**
> 运行（在仓库根目录）：`.venv\Scripts\python.exe scripts\debug\inject_error.py <场景名>`
> 列出全部场景：`.venv\Scripts\python.exe scripts\debug\inject_error.py --list`

## 为什么要注入

这 6 项验证卡在同一个地方：**触发条件不可控**。429 要真被限流、会员视频要真买会员、
Bot 检测要真被风控 —— 没法按需复现，等到了也不好复现第二次。

注入器只替换一个函数：`DownloadExecutor.execute`。它抛出一个字段完全真实的
`YtDlpExecutionError`（exit_code + 真实格式的 stderr）。从 `workers.py:749`
的 `diagnose()` 开始往下 —— 规则匹配、主因仲裁、retry 分流、`error.emit`、
UI 弹框、挂起/唤醒 —— **全都是真实代码**。唯一伪造的是 yt-dlp 的输出，
而那恰好是我们唯一没法按需制造的东西。

10 个场景的 stderr 已经离线跑过 `diagnose()`，确认每条都落在预期错误码上：

| 场景 | code | severity | retry | fix_action |
| --- | --- | --- | --- | --- |
| `429` | rate_limited_429 | recoverable | backoff | switch_proxy |
| `429-exhaust` | rate_limited_429 | recoverable | backoff | switch_proxy |
| `members` | members_only | fatal | **never** | extract_cookie |
| `removed` | video_removed | fatal | **never** | *(无)* |
| `age` | age_restricted | fatal | after_fix | extract_cookie |
| `bot` | bot_check_sign_in | fatal | after_fix | extract_cookie |
| `pot` | pot_provider_unavailable | recoverable | after_fix | refresh_pot |
| `nsig403` | http_403_forbidden | recoverable | backoff | **update_component** |
| `diskfull` | disk_full | fatal | never | change_download_dir |
| `unknown` | unknown | fatal | after_fix | *(无)* |

注入器启动时还会临时把 429 的退避 `base_sec` 从 30 秒压到 2 秒（写覆盖层 →
强制加载进单例 → 立刻删文件），否则第 3 次重试要等 120 秒。文件是删掉的，
即使 GUI 被强杀也不会污染下次真实运行。

---

## 断点位置总表

用 VSCode 调试运行 `scripts/debug/inject_error.py`（下面有 launch.json）。断点按验证项启用，
不要一次全开 —— Qt 线程里断住太多点会让 UI 看不出真实时序。

| # | 文件 | 行 | 断点条件 | 看什么 |
| --- | --- | --- | --- | --- |
| B1 | `download/workers.py` | 749 | 无 | `diag.code` / `diag.retry.policy` / `diag.severity` |
| B2 | `download/workers.py` | 759 | 无 | `diag.retry.is_automatic`、`self._auto_retries` |
| B3 | `download/workers.py` | 771 | 无 | `delay` 值、`_cancel_event.wait()` 的返回 |
| B4 | `download/workers.py` | 783 | 无 | 是否进入 never 分支 |
| B5 | `download/workers.py` | 790 | 无 | **是否走到挂起**（never 场景不该到这里） |
| B6 | `download/workers.py` | 802 | 无 | `self.suspend_action` |
| B7 | `download/workers.py` | 927 | 无 | DownloadFailed 收尾，不发 cancelled |
| B8 | `ui/components/home/download_card.py` | 439 | 无 | `err_data` 全字段 |
| B9 | `ui/components/home/download_card.py` | 462 | 无 | 走弹框还是 InfoBar |
| B10 | `ui/components/settings/fix_registry.py` | 107 | 无 | `refresh_pot` 是否拿到 `_start_pot_diagnose` |

---

## 验证项 1 —— 429 自动退避重试

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py 429
```

`fail_times=2`：前两次抛 429，第三次放行到真实 yt-dlp，所以能验到
**"重试成功后下载正常走完"**，而不只是"重试到耗尽"。

**断点**：B1 → B2 → B3

**逐步观察**

1. B1 命中（第 1 次）：`diag.code == "rate_limited_429"`，
   `diag.retry.policy == "backoff"`，`diag.retry.max_attempts == 3`，
   `diag.retry.base_sec == 2`（提速生效）。
2. B2：`diag.retry.is_automatic` 为 `True`，`self._auto_retries == 0`
   → 条件成立，进入自动重试。
3. B3：`delay == 2.0`（`base_sec * 2**0`）。放行，UI 上应出现
   **"第 1/3 次自动重试，2 秒后开始…"**，卡片状态是 parsing 不是 error。
4. B1 第 2 次命中：`self._auto_retries` 已是 `1`；B3 处 `delay == 4.0`
   （`2 * 2**1`），UI 显示 **"第 2/3 次自动重试"**。
5. 第 3 次：终端打印 `[inject] 失败预算用尽 → 放行到真实 yt-dlp`，
   B1 不再命中，下载正常完成。

**通过标准**：UI 全程没弹过错误框、没挂起；重试文案的 N/M 递增正确；
退避时长按 2→4 翻倍；最终下载成功。

### 1b. 退避途中取消

同一场景，跑到"第 2/3 次自动重试，4 秒后开始…"时**在 UI 上点取消**。

**断点**：只留 B3。

- 关键：`self._cancel_event.wait(timeout=delay)` 应**立即返回 `True`**，
  而不是等满 4 秒。单步过 `workers.py:772` 应抛 `DownloadCancelled`。
- 这是当初特意不用 `time.sleep` 的原因，值得亲眼确认一次。

**通过标准**：点下取消到卡片变"已取消"在 1 秒内，不是 4 秒。

### 1c. 重试耗尽

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py 429-exhaust
```

**断点**：B2 → B5

3 次自动重试后 B2 条件不再成立（`_auto_retries == 3`），落到 B5 挂起。
**通过标准**：耗尽后才弹框，`err_dict["auto_retries"] == 3`。

---

## 验证项 2 —— fatal 不挂起，批量队列继续

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py members
```

**准备**：在主页**一次加入 3 个任务**（随便什么链接，反正 execute 被劫持了），
让它们排队。这一项的重点就是队列不被卡住。

**断点**：B4（该命中）、B5（**绝不该命中** —— 这是本项的核心断言）、B7、B8

**逐步观察**

1. B4：`diag.retry.policy == "never"` 成立 → `error.emit(err_dict)` →
   `raise DownloadFailed`。
2. **B5 必须不命中。** 如果断在这里，说明 never 分流坏了，任务会挂起卡住队列。
3. B7：进 `except DownloadFailed`，只做 `_sweep_part_files()`，
   **不 emit `cancelled`** —— 卡片应显示"出错"，不能显示"任务已取消"。
4. B8：`err_data` 里 `code == "members_only"`、`user_title == "会员专属视频"`、
   `fix_action == "extract_cookie"`。
5. 关掉弹框后，**第 2、3 个任务应自动开始**，不需要任何操作。

**通过标准**：B5 不命中；三个任务依次失败跑完；卡片文案是"出错"不是"已取消"。

### 2b. 无 fix_action 的 never

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py removed
```

**断点**：B9。`fix_action` 为 `None` → 走 else 分支的 `InfoBar.error`，
**不弹 MessageBox**。验证的是「无从修复的错误不要用弹框打断用户」。

---

## 验证项 3 —— after_fix 仍然挂起，Cookie 修复可用

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py age
```

**准备**：临时把 `bin\cookies.txt` 改名（**不要删** —— 验完要还原）。

**断点**：B5、B6、B8

**逐步观察**

1. B5 命中：`is_suspended = True`，创建 `suspend_event`，
   `suspend_action` 初值 `"cancel"`。
2. B8：`recovery_hint == "导入 Cookie"` —— 这应该就是弹框主按钮上的字。
3. 放行，UI 弹框。**先不点**，确认卡片停在"挂起等待修复"、worker 线程阻塞在
   `suspend_event.wait()`（`workers.py:799`）。
4. 点主按钮 → `execute_fix_action("extract_cookie", ...)` → 跳设置页。
5. 在设置页完成 Cookie 提取，回到卡片点重试 → B6 命中：
   `self.suspend_action == "retry"`，且 `self._auto_retries` 被重置为 0
   （`workers.py:803`：用户已介入，自动重试预算重新给满）。

**通过标准**：挂起框出现；worker 确实阻塞不是轮询；点重试后从 B6 继续而非重建任务。

**收尾**：把 `cookies.txt` 改回来。

### 3b. POT 修复动作

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py pot
```

**断点**：B10（`fix_registry.py:107`）

`refresh_pot` 是本轮新加的动作，风险点在于 `pot_manager.try_recover()`
最坏要跑十几秒，**绝不能在 UI 线程里直接调**。B10 处确认 `starter` 拿到的是
设置页的 `_start_pot_diagnose`（已做线程封装），走 `starter(recover=True)`
那条 return，而不是掉到最后的 InfoBar 兜底。

**通过标准**：点按钮后 UI **不卡顿**（这是这一项唯一真正要看的东西），
设置页出现 POT 诊断进度。

---

## 验证项 4 —— 主因仲裁（nsig + 403）

```powershell
.venv\Scripts\python.exe scripts\debug\inject_error.py nsig403
```

单测 `test_companion_signal_redirects_fix_action` 已经覆盖了逻辑，
这一项只是**肉眼确认 UI 上的引导方向真的变了**，一分钟的事。

**断点**：B8

`err_data` 应该是：

- `code == "http_403_forbidden"`（主因仍是 403，没被改写）
- `fix_action == "update_component"`（**被伴随信号改写了**，原本是 switch_proxy）
- `recovery_hint == "去更新组件"`
- `extra_notes` 含"建议优先更新核心组件，而不是更换代理节点"

**通过标准**：弹框主按钮写的是**"去更新组件"**而不是"检查代理设置"。
这正是重构要解决的第 2 号问题 —— 旧实现会把用户引去换节点，方向是错的。

---

## 验证项 5 —— `.part-Frag` 容错不回归

**不用注入器**，跑真实下载：

```powershell
.venv\Scripts\python.exe main.py
```

下载一个**需要合并音视频**的普通视频（1080p 以上就会触发 merge）。

**断点**：`download/executor.py:385`（`if rc != 0:`）

- 正常情况 `rc == 0`，断点不该命中，下载直接成功 —— 这就够了。
- 若命中（Windows 上删 `.part-Frag` 失败时会），单步看关卡 1/2：
  `is_valid` 应为 `True`，走 `logger.warning("...但输出文件有效...")`，
  **不抛 `YtDlpExecutionError`**。

**为什么单列一项**：这段逻辑在 `YtDlpExecutionError` 抛出**之前**，本轮重构
一行没碰。列出来是为了确认"没碰"等于"没坏"。

**通过标准**：一次正常下载全程无错误提示，文件可播放。

---

## 验证项 6 —— 语言切换实时重翻译

**已自动验证通过，可跳过手测。**

翻译落地时跑过一次：装载 4 个 `.qm`，对 `members_only` / `http_403_forbidden` /
`disk_full` / `pot_provider_unavailable` 四个码调 `catalog.describe()`，
四种语言输出都正确切换：

```
zh_CN  会员专属视频          / en_US  Members-only video
ja_JP  メンバー限定動画      / zh_TW  會員專屬影片
```

这证明了 `catalog._translate()` 是**调用时求值**而非导入时定死 ——
也就是这一项原本要验的东西。

想在 GUI 里再看一眼的话：设置页切英文 → 跑 `inject_error.py members`
→ 弹框标题应为 "Members-only video"。

---

## 建议顺序

**1 → 2 → 3 → 4**（核心四项，注入器全覆盖），**5** 顺手跑一次正常下载，**6** 已过。

其中 **B5 在 `members` 场景不命中** 是整个重构最关键的单点断言 —— 它是
"批量队列不再被 fatal 错误卡死"的直接证据。如果只走一项，走这个。

---

## VSCode 调试配置

`.vscode/launch.json`（目前不存在，需新建）：

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "故障注入：429 自动重试",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/debug/inject_error.py",
      "args": ["429"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "故障注入：members 不挂起",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/debug/inject_error.py",
      "args": ["members"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "故障注入：age 挂起修复",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/debug/inject_error.py",
      "args": ["age"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "故障注入：nsig+403 仲裁",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/debug/inject_error.py",
      "args": ["nsig403"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "真实下载（验 part-Frag 容错）",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/main.py",
      "console": "integratedTerminal",
      "justMyCode": true
    }
  ]
}
```

> **Qt 线程断点**：`workers.py` 的断点落在 `QThread` 里，debugpy 能正常断住，
> 但断住期间 UI 会失去响应 —— 这是**正常**的，不是卡死。放行后 UI 会补上。

## 维护须知

**这个目录是留着复用的，不要删。** 以后改错误规则表、动 `workers.py` 的重试
分流、或加新的 `fix_action`，都可以直接拿这里的场景回归一遍。

放在 `scripts/` 而不是 `docs/` 是有原因的：`FluentYTDL.spec` 第 32 行把
**整个 `docs/` 打进发行版**，而 `scripts/` 不参与打包。这份手册留在 docs 下
会随安装包发给终端用户。

新增场景只要往 `inject_error.py` 的 `SCENARIOS` 里加一条，然后干跑确认它
真的落在你想要的错误码上 —— 注入器喂错样本的话，后面所有观察都是白做的：

```powershell
.venv\Scripts\python.exe -c "
import sys; sys.path.insert(0,'src'); sys.path.insert(0,'scripts/debug')
from inject_error import SCENARIOS
from fluentytdl.diagnostics.engine import diagnose
for n, sc in SCENARIOS.items():
    d = diagnose(sc['exit_code'], sc['stderr'], sc.get('parsed_json'))
    print(f'{n:14} {d.code:26} {d.severity:12} {d.retry.policy:10} {d.fix_action}')
"
```

`pot` 场景就是这么揪出来的：初版样本被 `bot_check_sign_in` 抢走了主因
（ERROR 优先于 WARNING，仲裁本身没错），换成 PO Token 的 ERROR 行才验得到
`refresh_pot`。

`.vscode/launch.json` 已被 `.gitignore:127` 的 `.vscode/` 覆盖，不进版本库。
