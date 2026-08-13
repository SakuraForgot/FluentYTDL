"""端到端验证用的故障注入启动器。

用法（在仓库根目录执行）：
    .venv\\Scripts\\python.exe scripts\\debug\\inject_error.py <场景名>
    .venv\\Scripts\\python.exe scripts\\debug\\inject_error.py --list

同目录的 README.md 是配套的断点验证手册（哪一项该断在哪、该看什么值）。

原理：把 ``DownloadExecutor.execute`` 换成一个会抛合成 ``YtDlpExecutionError``
的替身，然后正常启动 GUI。从 ``workers.py`` 的 ``except YtDlpExecutionError``
往下（diagnose → retry 分流 → error.emit → UI 弹框/挂起）全部是真实代码路径，
唯一被伪造的是 yt-dlp 的输出本身 —— 而那正是我们没法按需复现的东西。

``fail_times`` 用完后会回落到真实 execute，所以可以验证"自动重试成功后
下载继续走完"这条路径，而不只是"重试到耗尽"。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # scripts/debug/x.py → 仓库根
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # 供 `import main` 找到入口脚本

# ── 场景表 ───────────────────────────────────────────────
# stderr 取自 yt-dlp 真实输出格式；fail_times=-1 表示无限失败。
SCENARIOS: dict[str, dict] = {
    "429": {
        "desc": "限流 429 → backoff 自动重试；失败 2 次后放行，验证重试成功能续跑",
        "exit_code": 1,
        "fail_times": 2,
        "stderr": (
            "[youtube] Extracting URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
            "ERROR: [youtube] dQw4w9WgXcQ: Unable to download webpage: "
            "HTTP Error 429: Too Many Requests"
        ),
    },
    "429-exhaust": {
        "desc": "限流 429 → 重试耗尽后落入挂起流程（验证 backoff 的兜底分支）",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "ERROR: [youtube] dQw4w9WgXcQ: Unable to download webpage: "
            "HTTP Error 429: Too Many Requests"
        ),
    },
    "members": {
        "desc": "会员专属 → retry=never，应直接失败不挂起，批量队列继续下一个",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "[youtube] Extracting URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
            "ERROR: [youtube] dQw4w9WgXcQ: Join this channel to get access to "
            "members-only content like this video, and other exclusive perks."
        ),
    },
    "removed": {
        "desc": "视频已删除 → retry=never 且无 fix_action，走 InfoBar 而非弹框",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "ERROR: [youtube] dQw4w9WgXcQ: This video has been removed by the uploader"
        ),
    },
    "age": {
        "desc": "年龄限制 → retry=after_fix，应挂起弹框且「导入 Cookie」可用",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm your age. "
            "This video may be inappropriate for some users."
        ),
    },
    "bot": {
        "desc": "Bot 检测 → after_fix + extract_cookie，挂起弹框",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        ),
    },
    "pot": {
        "desc": "POT 服务不可用 → after_fix + refresh_pot，弹框按钮应触发设置页诊断",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "WARNING: [youtube] [pot] bgutil:http POT provider unavailable: "
            "Error reaching GET http://127.0.0.1:4416/ping (caused by "
            "ConnectionRefusedError)\n"
            "ERROR: [youtube] dQw4w9WgXcQ: Failed to fetch PO Token from bgutil "
            "provider; formats may be missing"
        ),
    },
    "nsig403": {
        "desc": "主因仲裁：nsig WARNING + 403 ERROR，引导应指向「去更新组件」而非换代理",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": (
            "WARNING: [youtube] dQw4w9WgXcQ: nsig extraction failed: "
            "Some formats may be missing\n"
            "         Install PhantomJS to workaround the issue.\n"
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        ),
    },
    "diskfull": {
        "desc": "磁盘满 → never + open_download_dir/change_download_dir",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": "ERROR: unable to write data: [Errno 28] No space left on device",
    },
    "unknown": {
        "desc": "规则全不命中 → 走兜底（HTTP 码表 / extractor 名提取）",
        "exit_code": 1,
        "fail_times": -1,
        "stderr": "ERROR: [bilibili] BV1xx411c7mD: 出了点小状况，请稍后再试 (code -352)",
    },
}

# ── 重试退避提速：把 base_sec 压到 2 秒，否则要等 30/60/120 秒 ──
FAST_BACKOFF = {
    "version": 999,
    "rules": [
        {
            "code": "rate_limited_429",
            "category": "network",
            "severity": "recoverable",
            "appliesTo": "error",
            "priority": 60,
            "retry": {"policy": "backoff", "max": 3, "base_sec": 2},
            "fix": "switch_proxy",
            "patterns": [
                {"kind": "substr", "value": "HTTP Error 429"},
                {"kind": "substr", "value": "Too Many Requests"},
            ],
        }
    ],
}


def _install_fast_backoff() -> None:
    """写覆盖层 → 强制加载进单例缓存 → 立刻删文件。

    规则表是进程内单例（``rules._cached``），加载后就常驻内存，所以文件可以马上
    删掉。这样即便 GUI 被强杀，也不会有残留文件污染下次真实运行。
    """
    import json

    from fluentytdl.diagnostics import rules

    path = rules._override_rules_path()
    path.write_text(json.dumps(FAST_BACKOFF, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        rs = rules.get_rule_set()  # 触发加载 + 缓存
        r = rs.by_code("rate_limited_429")
        print(f"[inject] 退避提速已生效: base_sec={r.retry.base_sec} max={r.retry.max_attempts}")
    finally:
        path.unlink(missing_ok=True)
        print(f"[inject] 覆盖层文件已删除: {path}")


def _install(scenario: dict) -> None:
    from fluentytdl.download.executor import DownloadExecutor
    from fluentytdl.models.errors import YtDlpExecutionError

    original = DownloadExecutor.execute
    budget = {"left": scenario["fail_times"]}

    def fake_execute(self, url, ydl_opts, **kwargs):
        if budget["left"] == 0:
            print("[inject] 失败预算用尽 → 放行到真实 yt-dlp")
            return original(self, url, ydl_opts, **kwargs)
        if budget["left"] > 0:
            budget["left"] -= 1
        print(f"[inject] 抛出合成错误 (剩余预算 {budget['left']}): {url}")
        raise YtDlpExecutionError(
            exit_code=scenario["exit_code"],
            stderr=scenario["stderr"],
            parsed_json=scenario.get("parsed_json"),
        )

    DownloadExecutor.execute = fake_execute


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv or not args:
        print("可用场景：")
        for name, sc in SCENARIOS.items():
            print(f"  {name:14} {sc['desc']}")
        return

    name = args[0]
    scenario = SCENARIOS.get(name)
    if scenario is None:
        print(f"未知场景 {name!r}，用 --list 查看全部")
        sys.exit(2)

    print(f"[inject] 场景 = {name}：{scenario['desc']}")
    _install_fast_backoff()
    _install(scenario)

    sys.argv = [sys.argv[0]]  # 别把场景名透给 Qt
    import main as app_main

    app_main.main()


if __name__ == "__main__":
    main()
