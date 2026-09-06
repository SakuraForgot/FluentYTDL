#!/usr/bin/env python3
"""
FluentYTDL 打包工具 GUI

基于 PySide6 的图形化构建界面，是 ``scripts/build.py`` 的前端外壳 ——
所有实际构建逻辑都在 build.py 里，这里只负责拼命令行、跑子进程、渲染日志。

功能：
- 选择构建目标（全部 / 便携版 / 增量包 / 安装向导），产物清单直接读 build.py
- 构建前环境自检（PyInstaller / 7z / ISCC / 外部工具）
- 构建前可选清空 release/，让输出目录只剩本次目标的产物
- 实时流式日志
- 可中断（杀整棵进程树，不留孤儿 PyInstaller/ISCC）
- 构建完成后列出 release/ 下的产物与体积，并标出历史遗留

--------------------------------------------------------------------------
关于「选了目标却像全部打包」
--------------------------------------------------------------------------
曾经的成因（已在 build.py 侧修掉，别改回去）：

  · run_all() 里三处 `if target in ("all", "7z")` 各写一遍，选 7z 也会产
    app-core.7z；
  · generate_update_manifest() / generate_checksums() 是**无条件**执行的；
  · generate_checksums() 遍历整个 release/，把历史遗留文件一并列进
    SHA256SUMS.txt。

现在 build.py 有一张 TARGET_OUTPUTS 表作为唯一事实源，本文件 import 它 ——
界面上列出的每个文件名都是 run_all() 真会落盘的那一个。选「便携版」就只出
full.7z，没有 app-core、没有 manifest、没有 SHA256SUMS。

仍然改不了的一点：--target **不减少前置编译**。任何目标都要先跑完
build_spec() + build_updater() + bundle_tools()，因为 full.7z 和 setup.exe
都需要完整的 dist/。SHARED_NOTE 就是在讲这件事，别把它删了。

另外 build.py 的 clean() 只清 dist/ 与 build/，从不清 release/ —— 上次构建
的产物会留在原地，所以有「构建前清空 release/」这个开关和残留文件告警。

用法:
    python scripts/build_gui.py

--------------------------------------------------------------------------
关于 UI 框架的说明（CLAUDE.md §3 的显式豁免）
--------------------------------------------------------------------------
CLAUDE.md 要求所有 UI 必须使用 QFluentWidgets。本文件**刻意**使用原生 QtWidgets：

  这是一个开发者构建工具，不随产品分发，也不属于 src/fluentytdl 包。
  打包器反向依赖被打包程序的 UI 库会造成循环：QFluentWidgets 一旦损坏或
  版本冲突，连打包工具本身都起不来，无法构建出修复版本。

因此这里的原生 Qt + 硬编码深色配色是经评估后保留的选择，不是遗漏。
新增 UI 组件时请继续使用原生 QtWidgets，不要引入 qfluentwidgets。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 确保可以导入项目模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# scripts/ 也要显式入栈：直接跑脚本时 sys.path[0] 本来就是它，但被 import
# 或用 -m 拉起来时不是，而下面要从 build.py 读目标定义
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 构建目标定义**不在本文件**，从 build.py 共读（原因见下方 TARGET_OUTPUTS 说明）。
# 直接 import 而不做 try/except 兜底：build.py 只依赖标准库和 scripts/ 下的两个
# 兄弟模块，它要是 import 不了，`python scripts/build.py` 同样跑不起来，这个 GUI
# 也就没有存在意义 —— 此时一个指名道姓的 traceback 比静默降级有用。
from build import TARGET_OUTPUTS, release_names  # noqa: E402
from PySide6.QtCore import QEvent, QObject, QThread, Signal  # noqa: E402
from PySide6.QtGui import QFont, QIcon, QTextCursor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ============================================================================
# 构建目标定义
#
# 目标 → 产出物的映射是 build.py 的 TARGET_OUTPUTS，本文件只负责给每种产出物
# 配一句面向用户的说明。以前 GUI 自己抄了一份产物清单，和 run_all() 的真实分发
# 对不上：选「便携版」时界面说只出两个包，实际还会吐 app-core.7z、
# update-manifest.json 和一份把 release/ 里所有历史文件都列进去的
# SHA256SUMS.txt。抄清单必然漂移，所以改成共读一张表 —— 别在这里重建清单。
# ============================================================================

# 各产出物面向用户的说明。键必须覆盖 build.py OUTPUT_FILENAMES 的全部 kind
OUTPUT_NOTES = {
    "full": "便携完整版，解压即用，内含 bin/ 全部外部工具",
    "app-core": "增量更新内部包，不含 bin/ 与 updater.exe，单独解压跑不起来",
    "setup": "Inno Setup 安装向导，写注册表与快捷方式，需要 ISCC",
    "manifest": "程序内更新器读取的版本清单（只在产出 app-core 时才有意义）",
    "checksums": "本次产出文件的校验和",
}

# 所有目标共同承担的前置编译 —— 选目标省不掉这段耗时
SHARED_NOTE = (
    "任何目标都会先完整编译一次主程序与 updater.exe、校验并注入 assets/bin 外部工具。"
    "目标只决定最后产出哪些发布物，<b>不会缩短编译时间</b>。"
)

TARGETS = [
    {"value": "all", "label": "全部 (all) — 完整发布物，CI 发版走的就是这个"},
    {"value": "7z", "label": "便携版 (7z) — 只出 full.7z，其余一概不产"},
    {"value": "app-core", "label": "增量包 (app-core) — 只出 app-core.7z + 更新清单"},
    {"value": "setup", "label": "安装向导 (setup) — 只出 setup.exe"},
]

for _t in TARGETS:
    # ISCC 只有产出 setup.exe 时才需要，由表推导，不手写
    _t["needs_iscc"] = "setup" in TARGET_OUTPUTS[_t["value"]]

# VERSION 文件的格式约束，与 version_manager.parse_version 保持一致
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-(?:rc|beta)\.\d+)?$")

ISCC_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe",
    Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
    Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
]


def read_version_file() -> str:
    """读 VERSION 文件（唯一 source of truth）。"""
    version_file = ROOT / "VERSION"
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    return ""


def find_iscc() -> Path | None:
    return next((p for p in ISCC_CANDIDATES if p.exists()), None)


def release_dir() -> Path:
    return ROOT / "release"


def stale_artifacts(target: str, version: str) -> list[Path]:
    """release/ 里不属于本次目标产出的残留文件。

    build.py 的 clean() 只清 dist/ 与 build/，release/ 从不清。上一次构建的
    app-core.7z / setup.exe / SHA256SUMS.txt 会原地留着，看上去就像"选了便携版
    却打了全部"。这里只负责点名，删不删由用户勾「构建前清空 release/」决定。

    比对的是**完整文件名**而不是后缀：版本号不同的 full.7z 同样是残留，
    按后缀匹配会把上个版本的包误判成本次产物。
    """
    rel = release_dir()
    if not rel.exists():
        return []
    mine = set(release_names(target, version).values())
    return sorted(
        f for f in rel.iterdir() if f.is_file() and f.name not in mine and f.name != ".gitkeep"
    )


# ============================================================================
# 工作线程
# ============================================================================


class BuildSignals(QObject):
    """构建信号"""

    output = Signal(str)
    finished = Signal(int)  # exit code


class BuildWorker(QThread):
    """后台构建工作线程"""

    def __init__(self, command: list[str], cwd: Path | None = None):
        super().__init__()
        # signals 必须是实例属性。作为类属性时所有 worker 共享同一个信号对象，
        # 每次构建又 connect 一遍 → 第二次日志翻倍、第三次三倍。
        self.signals = BuildSignals()
        self.command = command
        self.cwd = cwd or ROOT
        self._process: subprocess.Popen | None = None

    def run(self):
        try:
            # PYTHONUNBUFFERED: 管道模式下 Python 默认块缓冲，不设这个日志会成块蹦出
            # PYTHONIOENCODING: 子进程里的 emoji / 中文在 GBK 控制台会炸
            env = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }

            # CREATE_NO_WINDOW: 否则每个子进程闪一个黑控制台
            # CREATE_NEW_PROCESS_GROUP: 便于取消时按进程组处理
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

            self._process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.cwd),
                bufsize=1,
                env=env,
                creationflags=creationflags,
                start_new_session=sys.platform != "win32",
            )

            if self._process.stdout:
                for line in iter(self._process.stdout.readline, ""):
                    if line:
                        self.signals.output.emit(line.rstrip())

            self._process.wait()
            self.signals.finished.emit(self._process.returncode or 0)

        except Exception as e:
            self.signals.output.emit(f"❌ 错误: {e}")
            self.signals.finished.emit(1)

    def terminate_process(self):
        """杀掉整棵进程树。

        只 terminate() 父 Python 进程是不够的 —— PyInstaller / ISCC / 7z 是它的
        子进程，会继续跑并占住 dist/，让下一次构建的 rmtree 失败。
        """
        proc = self._process
        if not proc or proc.poll() is not None:
            return

        if sys.platform == "win32":
            # /T 连子孙进程一起杀，/F 强制
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                proc.terminate()
        else:
            import signal

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ============================================================================
# 环境自检
# ============================================================================


def check_environment(target: str) -> list[tuple[bool, str]]:
    """构建前环境自检。

    返回 [(是否致命, 描述)]。致命项会在启动构建前拦截 —— 以前全靠 build.py
    跑到一半才失败，用户得翻几百行日志才知道是缺了 ISCC。

    是否致命取决于构建目标：只做便携包时缺 ISCC 无所谓。
    """
    spec = next(t for t in TARGETS if t["value"] == target)
    results: list[tuple[bool, str]] = []

    # PyInstaller —— 任何目标都必须有
    try:
        import importlib.metadata

        ver = importlib.metadata.version("pyinstaller")
        results.append((False, f"✓ PyInstaller {ver}"))
    except Exception:
        results.append(
            (
                True,
                "❌ 未安装 PyInstaller —— 运行 `uv sync --extra dev` 或 `pip install pyinstaller`",
            )
        )

    # 压缩器：7z 命令行优先，py7zr 是纯 Python 回退
    sevenzip = shutil.which("7z") or shutil.which("7za")
    if sevenzip:
        results.append((False, f"✓ 7-Zip: {sevenzip}"))
    else:
        try:
            import importlib

            importlib.import_module("py7zr")
            results.append((False, "✓ py7zr（未找到 7z 命令行，将走纯 Python 回退，速度较慢）"))
        except ImportError:
            fatal = spec["value"] in ("all", "7z")
            results.append(
                (
                    fatal,
                    "❌ 既没有 7z 命令行也没有 py7zr —— 安装 7-Zip 或 `pip install py7zr`",
                )
            )

    # Inno Setup —— 只有含 setup 的目标才需要
    if spec["needs_iscc"]:
        iscc = find_iscc()
        if iscc:
            results.append((False, f"✓ Inno Setup: {iscc}"))
        else:
            results.append(
                (
                    True,
                    "❌ 未找到 ISCC.exe —— 安装 Inno Setup 6 (https://jrsoftware.org/isdl.php)，"
                    "或改选「便携版 (7z)」目标",
                )
            )
    else:
        results.append((False, "· 当前目标不需要 Inno Setup，跳过"))

    # release/ 历史遗留 —— "选了目标却像全部打包" 的直接成因
    stale = stale_artifacts(target, read_version_file() or "")
    if stale:
        results.append(
            (
                False,
                f"⚠ release/ 存在 {len(stale)} 个不属于本次目标的文件"
                f"（{', '.join(f.name for f in stale[:3])}"
                f"{' 等' if len(stale) > 3 else ''}）—— 本次不会重建也不会删除，"
                "留在那里容易被误认成本次产物。勾选「构建前清空 release/」可避免。",
            )
        )

    # 外部工具 —— setup 目标同样要打进安装包，所以一律检查
    tools_lock = ROOT / "scripts" / "TOOLS.lock.json"
    missing_tools = [
        name
        for name, rel in [
            ("yt-dlp", "yt-dlp/yt-dlp.exe"),
            ("ffmpeg", "ffmpeg/ffmpeg.exe"),
            ("deno", "deno/deno.exe"),
            ("AtomicParsley", "atomicparsley/AtomicParsley.exe"),
            ("POT Provider", "pot-provider/bgutil-pot-provider.exe"),
        ]
        if not (ROOT / "assets" / "bin" / rel).exists()
    ]
    if missing_tools:
        results.append(
            (
                False,
                f"⚠ assets/bin 缺少 {', '.join(missing_tools)} —— 构建时会自动下载"
                "（点「📥 下载工具」可提前拉取）",
            )
        )
    else:
        results.append((False, "✓ assets/bin 外部工具齐备"))

    if tools_lock.exists():
        results.append((False, "✓ scripts/TOOLS.lock.json 存在，构建时会校验工具哈希"))
    else:
        results.append((False, "⚠ 无 scripts/TOOLS.lock.json —— 首次构建会生成初始锁文件"))

    # 版本文件
    version = read_version_file()
    if version and VERSION_PATTERN.match(version):
        results.append((False, f"✓ VERSION = {version}"))
    elif version:
        results.append((True, f"❌ VERSION 文件内容不符合规范: '{version}'（期望 X.Y.Z[-rc.N]）"))
    else:
        results.append((True, "❌ VERSION 文件缺失或为空"))

    # 环境污染黑名单（与 build.py _check_hygiene 同源，这里只提示不拦截）
    try:
        import importlib.metadata

        installed = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
        polluted = installed & {"torch", "pandas", "tensorflow", "scipy", "matplotlib"}
        if polluted:
            results.append(
                (
                    False,
                    f"⚠ 环境中存在重型依赖 {', '.join(sorted(polluted))} —— "
                    "会显著增大产物体积，build.py 会拦截（可勾选 --skip-hygiene 强行打包）",
                )
            )
        else:
            results.append((False, "✓ 未发现黑名单重型依赖"))
    except Exception:
        pass

    return results


# ============================================================================
# 主窗口
# ============================================================================


class BuildGUI(QMainWindow):
    """构建工具主窗口"""

    def __init__(self):
        super().__init__()
        self.worker: BuildWorker | None = None
        # 取消标志：worker 被杀掉后仍会发 finished(1)，不加这个的话
        # _on_finished 会把"已取消"覆写成"构建失败 (code: 1)"
        self._cancelled = False
        # 区分「跑完整构建」与「跑单个辅助脚本」—— 只有前者结束后列产物
        self._is_build = False
        self._setup_ui()
        self._connect_signals()
        self._on_target_changed(0)

    def _setup_ui(self):
        self.setWindowTitle("FluentYTDL 打包工具")
        self.setMinimumSize(760, 620)

        # 尝试设置图标
        icon_path = ROOT / "assets" / "FluentYTDL_v2.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # 主布局
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # === 构建目标区域 ===
        target_group = QGroupBox("📦 构建目标")
        target_layout = QVBoxLayout(target_group)

        # 目标选择
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("发布产物:"))
        self.target_combo = QComboBox()
        self.target_combo.addItems([t["label"] for t in TARGETS])
        self.target_combo.setMinimumWidth(380)
        # 滚轮划过下拉框会静默改掉构建目标，用户毫无察觉就打错了包。
        # focusPolicy 只挡未聚焦时的滚轮，装事件过滤器才是彻底的。
        self.target_combo.installEventFilter(self)
        target_row.addWidget(self.target_combo)
        target_row.addStretch()
        target_layout.addLayout(target_row)

        # 前置编译说明 —— 目标不影响编译耗时，这点必须说在前面
        shared_label = QLabel(f"⚠ {SHARED_NOTE}")
        shared_label.setWordWrap(True)
        shared_label.setStyleSheet("color: #cca700;")
        target_layout.addWidget(shared_label)

        # 该目标的实际产出清单 —— 直接对齐 build.py run_all() 的行为
        self.outputs_label = QLabel()
        self.outputs_label.setWordWrap(True)
        self.outputs_label.setStyleSheet(
            "color: #9cdcfe; background-color: #252526; border: 1px solid #3c3c3c;"
            " border-radius: 4px; padding: 8px;"
        )
        target_layout.addWidget(self.outputs_label)

        # 打包配置选项
        options_row = QHBoxLayout()
        self.skip_hygiene_cb = QCheckBox("跳过环境污染体检 (--skip-hygiene)")
        self.skip_hygiene_cb.setToolTip(
            "开启后，即使环境中安装了黑名单依赖（如 torch, pandas）也将强行打包"
        )
        options_row.addWidget(self.skip_hygiene_cb)

        self.strict_tools_cb = QCheckBox("锁定外部工具版本 (--strict-tools)")
        self.strict_tools_cb.setToolTip(
            "要求 assets/bin 下的工具版本与 scripts/TOOLS.lock.json 完全一致。\n"
            "上游发新版会导致构建失败，需先运行 fetch_tools.py --update-lock 确认升级。"
        )
        options_row.addWidget(self.strict_tools_cb)
        options_row.addStretch()
        target_layout.addLayout(options_row)

        clean_row = QHBoxLayout()
        self.clean_release_cb = QCheckBox("构建前清空 release/")
        self.clean_release_cb.setToolTip(
            "build.py 的 clean() 只清 dist/ 与 build/，从不清 release/。\n"
            "不清的话，上一次构建留下的产物会留在原地、并被 SHA256SUMS.txt 重新收录，\n"
            "让「仅安装向导」这类目标看起来像是全部打包了一遍。\n"
            "勾选后删除前会列出待删文件并二次确认。"
        )
        clean_row.addWidget(self.clean_release_cb)
        clean_row.addStretch()
        target_layout.addLayout(clean_row)

        # 版本号
        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("版本号:"))
        self.version_edit = QLineEdit()
        current = read_version_file() or "未知"
        # 留空 = 不传 --version = build.py 不回写 VERSION 文件（见 P0-1 守卫）
        self.version_edit.setPlaceholderText(f"留空则使用 VERSION 文件 ({current})")
        self.version_edit.setToolTip(
            "裸版本号，不带 v 前缀。格式: X.Y.Z / X.Y.Z-rc.N / X.Y.Z-beta.N\n"
            "留空时不会改写 VERSION 文件；填了则会同步写入所有版本载体。"
        )
        self.version_edit.setMaximumWidth(240)
        version_row.addWidget(self.version_edit)
        version_row.addStretch()
        target_layout.addLayout(version_row)

        layout.addWidget(target_group)

        # === 快捷操作区域 ===
        #   只保留构建流程之外确实需要手动触发的动作。
        #   「生成校验和」被移除：build.py 每次构建结束都会无条件调用
        #   generate_checksums() 重写 SHA256SUMS.txt，单独按一次只是把
        #   release/ 里现有文件重算一遍，永远拿不到新信息。
        actions_group = QGroupBox("🔧 快捷操作")
        actions_layout = QHBoxLayout(actions_group)

        self.btn_env_check = QPushButton("🩺 环境自检")
        self.btn_env_check.setToolTip("检查 PyInstaller / 7z / Inno Setup / 外部工具是否就绪")
        actions_layout.addWidget(self.btn_env_check)

        self.btn_fetch_tools = QPushButton("📥 下载工具")
        self.btn_fetch_tools.setToolTip("下载并校验 yt-dlp / ffmpeg / deno / AtomicParsley / POT")
        actions_layout.addWidget(self.btn_fetch_tools)

        self.btn_collect_licenses = QPushButton("📄 收集许可证")
        self.btn_collect_licenses.setToolTip(
            "刷新 licenses/ 下的第三方许可证。\n"
            "build.py 只是把 licenses/ 原样拷进产物，从不自己去拉取 —— "
            "这里是更新随包许可证的唯一入口。"
        )
        actions_layout.addWidget(self.btn_collect_licenses)

        self.btn_open_release = QPushButton("📂 打开输出目录")
        self.btn_open_release.setToolTip("打开 release 文件夹")
        actions_layout.addWidget(self.btn_open_release)

        actions_layout.addStretch()
        layout.addWidget(actions_group)

        # === 日志区域 ===
        log_group = QGroupBox("📋 构建日志")
        log_layout = QVBoxLayout(log_group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
            }
        """)
        log_layout.addWidget(self.log_text)

        layout.addWidget(log_group)

        # === 状态栏 ===
        status_layout = QHBoxLayout()

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 不确定进度
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(20)
        status_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: #888;")
        status_layout.addWidget(self.status_label)

        status_layout.addStretch()

        self.btn_build = QPushButton("🚀 开始构建")
        self.btn_build.setMinimumWidth(120)
        self.btn_build.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1084d8;
            }
            QPushButton:pressed {
                background-color: #006cbd;
            }
            QPushButton:disabled {
                background-color: #555;
                color: #888;
            }
        """)
        status_layout.addWidget(self.btn_build)

        self.btn_cancel = QPushButton("⏹ 取消")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #d83b01;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
            }
            QPushButton:hover {
                background-color: #ea4a1f;
            }
        """)
        status_layout.addWidget(self.btn_cancel)

        layout.addLayout(status_layout)

    def _connect_signals(self):
        self.btn_build.clicked.connect(self._start_build)
        self.btn_cancel.clicked.connect(self._cancel_build)
        self.btn_env_check.clicked.connect(lambda: self._run_env_check(interactive=True))
        self.btn_fetch_tools.clicked.connect(lambda: self._run_script("fetch_tools.py"))
        self.btn_collect_licenses.clicked.connect(lambda: self._run_script("collect_licenses.py"))
        self.btn_open_release.clicked.connect(self._open_release_dir)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)

    def eventFilter(self, obj, event):
        """吞掉构建目标下拉框上的滚轮事件。

        Qt 默认让滚轮切换 QComboBox 的当前项。用户滚一下窗口就可能把
        "安装向导" 划成 "全部"，而且下拉框只显示一行、改动毫无提示 ——
        等发现时已经打完一个错的包了。
        """
        if obj is self.target_combo and event.type() == QEvent.Type.Wheel:
            return True
        return super().eventFilter(obj, event)

    def _on_target_changed(self, index: int):
        """切换目标时更新产物清单说明。

        清单来自 build.py 的 release_names()，不是手写的 —— 界面上写的每一个
        文件名，都是 run_all() 真的会落盘的那一个，一个不多一个不少。
        """
        target = TARGETS[index]["value"]
        version = read_version_file() or "{version}"
        names = release_names(target, version)
        lines = "<br>".join(
            f"　• {name}<span style='color:#808080'>（{OUTPUT_NOTES[kind]}）</span>"
            for kind, name in names.items()
        )
        self.outputs_label.setText(f"<b>本次目标产出（共 {len(names)} 项）：</b><br>{lines}")

    def _log(self, text: str, color: str | None = None):
        """添加日志"""
        if color:
            text = f'<span style="color:{color}">{text}</span>'
        self.log_text.append(text)
        # 滚动到底部
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)

    def _set_ui_running(self, running: bool):
        """设置 UI 运行状态"""
        self.btn_build.setEnabled(not running)
        self.btn_build.setVisible(not running)
        self.btn_cancel.setVisible(running)
        self.progress_bar.setVisible(running)
        self.target_combo.setEnabled(not running)
        self.skip_hygiene_cb.setEnabled(not running)
        self.strict_tools_cb.setEnabled(not running)
        self.clean_release_cb.setEnabled(not running)
        self.version_edit.setEnabled(not running)
        self.btn_env_check.setEnabled(not running)
        self.btn_fetch_tools.setEnabled(not running)
        self.btn_collect_licenses.setEnabled(not running)

    def _get_target(self) -> str:
        """获取选择的构建目标"""
        return TARGETS[self.target_combo.currentIndex()]["value"]

    def _run_env_check(self, interactive: bool = False) -> bool:
        """执行环境自检并把结果逐条写进日志。返回是否全部通过。"""
        target = self._get_target()
        if interactive:
            self.log_text.clear()
        self._log(f"🩺 环境自检（目标: {target}）", "#4ec9b0")

        results = check_environment(target)
        for fatal, message in results:
            if fatal:
                self._log(f"   {message}", "#f14c4c")
            elif message.startswith("⚠"):
                self._log(f"   {message}", "#cca700")
            elif message.startswith("·"):
                self._log(f"   {message}", "#808080")
            else:
                self._log(f"   {message}", "#6a9955")

        blockers = [m for fatal, m in results if fatal]
        if blockers:
            self._log(f"\n❌ 自检未通过：{len(blockers)} 项必须先解决", "#f14c4c")
            self.status_label.setText("❌ 环境自检未通过")
            self.status_label.setStyleSheet("color: #f14c4c;")
        else:
            self._log("\n✅ 环境自检通过", "#6a9955")
            if interactive:
                self.status_label.setText("✅ 环境自检通过")
                self.status_label.setStyleSheet("color: #6a9955;")
        self._log("")
        return not blockers

    def _validate_version(self, version: str) -> str | None:
        """校验版本框输入，返回错误信息（None 表示通过）。"""
        if version.startswith("v"):
            return (
                f"版本号不能带 v 前缀：'{version}'\n\n"
                f"v 只存在于 Git tag 里。请填 '{version.lstrip('v')}'。"
            )
        if not VERSION_PATTERN.match(version):
            return (
                f"版本号格式不合规：'{version}'\n\n"
                "期望 X.Y.Z / X.Y.Z-rc.N / X.Y.Z-beta.N\n"
                "例如: 3.5.6、3.5.6-rc.1、3.6.0-beta.2"
            )
        return None

    def _start_build(self):
        """开始构建"""
        target = self._get_target()
        version = self.version_edit.text().strip()
        skip_hygiene = self.skip_hygiene_cb.isChecked()
        strict_tools = self.strict_tools_cb.isChecked()

        # 版本格式先在这里拦，别把非法值丢给 build.py 再让用户翻日志
        if version:
            error = self._validate_version(version)
            if error:
                QMessageBox.warning(self, "版本号无效", error)
                self.version_edit.setFocus()
                self.version_edit.selectAll()
                return

        self.log_text.clear()

        # 构建前自检：缺 ISCC 之类的问题在启动前就拦下来
        if not self._run_env_check():
            QMessageBox.critical(
                self,
                "环境自检未通过",
                "构建所需的组件缺失，详见日志区。\n请先解决标红项再开始构建。",
            )
            self._set_ui_running(False)
            return

        # 清空 release/ —— 删除前列清单并二次确认，别偷偷删用户的产物
        if self.clean_release_cb.isChecked() and not self._clear_release_dir():
            return

        self._log(f"🚀 开始执行编排流水线: 目标 {target}", "#4ec9b0")
        if version:
            self._log(f"   覆盖版本号: {version}（将写入 VERSION 及全部版本载体）", "#cca700")
        else:
            self._log(f"   使用 VERSION 文件: {read_version_file()}（不会改写）", "#808080")
        if skip_hygiene:
            self._log("   ! 警告: 已跳过无菌环境体检", "#cca700")
        if strict_tools:
            self._log("   外部工具锁定模式: 版本须与 TOOLS.lock.json 一致", "#808080")
        self._log("")

        cmd = [sys.executable, str(ROOT / "scripts" / "build.py"), "--target", target]
        if version:
            cmd.extend(["--version", version])
        if skip_hygiene:
            cmd.append("--skip-hygiene")
        if strict_tools:
            cmd.append("--strict-tools")

        self._is_build = True
        self._run_command(cmd)

    def _clear_release_dir(self) -> bool:
        """清空 release/。返回 False 表示用户取消，构建应中止。"""
        rel = release_dir()
        files = sorted(f for f in rel.iterdir() if f.is_file()) if rel.exists() else []
        if not files:
            self._log("🧹 release/ 已是空目录，无需清理", "#808080")
            return True

        listing = "\n".join(f"  • {f.name}" for f in files[:15])
        if len(files) > 15:
            listing += f"\n  … 另有 {len(files) - 15} 个文件"
        reply = QMessageBox.question(
            self,
            "清空 release/",
            f"将删除 release/ 下 {len(files)} 个文件：\n\n{listing}\n\n此操作不可撤销，继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._log("已取消（未清空 release/，构建未开始）", "#cca700")
            self.status_label.setText("已取消")
            self.status_label.setStyleSheet("color: #cca700;")
            return False

        self._log(f"🧹 清空 release/（{len(files)} 个文件）...", "#d7ba7d")
        failed = []
        for f in files:
            try:
                f.unlink()
            except OSError as e:
                failed.append(f"{f.name}: {e}")
        if failed:
            for msg in failed:
                self._log(f"   ⚠ 删除失败 {msg}", "#cca700")
        self._log(f"   ✓ 已删除 {len(files) - len(failed)}/{len(files)} 个文件", "#6a9955")
        self._log("")
        return True

    def _run_script(self, script_name: str):
        """运行指定脚本"""
        self.log_text.clear()
        self._log(f"🔧 运行: {script_name}", "#4ec9b0")
        self._log("")

        cmd = [sys.executable, str(ROOT / "scripts" / script_name)]
        self._is_build = False
        self._run_command(cmd)

    def _run_command(self, cmd: list[str]):
        """运行命令"""
        # 上一个 worker 若还挂着（取消后 finished 尚未投递等情形），先收干净，
        # 免得两个 worker 同时往同一个日志区写、且只有一个能被取消。
        if self.worker is not None:
            self._reclaim_worker()

        self._cancelled = False
        self._set_ui_running(True)
        self.status_label.setText("正在执行...")
        self.status_label.setStyleSheet("color: #888;")

        self.worker = BuildWorker(cmd)
        self.worker.signals.output.connect(self._on_output)
        self.worker.signals.finished.connect(self._on_finished)
        self.worker.start()

    def _cancel_build(self):
        """取消构建"""
        if self.worker:
            self._cancelled = True
            self._log("\n⏹ 用户取消，正在结束进程树...", "#d7ba7d")
            self.worker.terminate_process()
            # 不调 quit()：BuildWorker 重写了 run()、没有事件循环，quit() 是空操作。
            # 真正让线程结束的是 terminate_process() 让 readline 收到 EOF。
            self.worker.wait(15000)

            # 在这里就地回收，不要等 _on_finished ——
            # finished 是从工作线程 emit 的队列连接，wait() 返回时它还排在事件队列里。
            # 此刻 UI 已经解锁，用户完全来得及按下「开始构建」；那条迟到的
            # _on_finished 随后会把 self.worker（已经指向新 worker）置成 None，
            # 新构建就此失联：取消按钮点了没反应，QThread 还可能被 GC 掉。
            self._reclaim_worker()

            # 重置UI状态
            self._set_ui_running(False)
            self.status_label.setText("已取消")
            self.status_label.setStyleSheet("color: #cca700;")

    def closeEvent(self, event):
        """关窗时确保子进程树被清理。

        直接关窗会销毁仍在运行的 QThread，而 PyInstaller / ISCC / 7z 是它的
        子进程 —— 它们会继续跑并占住 dist/，下一次构建的 rmtree 直接失败。
        """
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "构建仍在进行",
                "构建尚未结束。关闭窗口将终止整棵进程树，本次构建产物不完整。\n\n确定关闭？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._cancelled = True
            self.worker.terminate_process()
            self.worker.wait(15000)
        event.accept()

    def _on_output(self, text: str):
        """处理输出"""
        # 颜色化输出
        if text.startswith("✓") or text.startswith("✅"):
            self._log(text, "#6a9955")
        elif text.startswith("❌") or "错误" in text or "Error" in text:
            self._log(text, "#f14c4c")
        elif text.startswith("⚠") or "警告" in text or "Warning" in text:
            self._log(text, "#cca700")
        elif text.startswith("🔨") or text.startswith("📦"):
            self._log(text, "#4fc1ff")
        elif text.startswith("🔒") or text.startswith("🔄"):
            self._log(text, "#d7ba7d")
        elif text.startswith("==="):
            self._log(text, "#c586c0")
        else:
            self._log(text)

    def _list_artifacts(self):
        """构建成功后列出 release/ 下的实际产物与体积。"""
        rel = release_dir()
        if not rel.exists():
            return
        files = sorted(
            (f for f in rel.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_size,
            reverse=True,
        )
        if not files:
            return
        # 标注哪些是本次目标真正产出的 —— 其余是 release/ 里的历史遗留。
        # 现在 SHA256SUMS.txt 只收录本次产物，残留文件不再被算进校验和。
        mine = set(release_names(self._get_target(), read_version_file() or "").values())
        self._log("\n📂 release/ 产物：", "#4fc1ff")
        for f in files:
            size = f.stat().st_size / 1024 / 1024
            if f.name in mine:
                self._log(f"   {f.name:<52} {size:>8.2f} MB", "#9cdcfe")
            else:
                self._log(f"   {f.name:<52} {size:>8.2f} MB  ← 历史遗留，非本次目标", "#cca700")

    def _reclaim_worker(self):
        """断开并回收当前 worker。

        finished 是 run() 最后一行发出的 —— 此刻线程还没真正退出，直接
        deleteLater() 会撞上 "QThread: Destroyed while thread is still
        running"。先 wait() 让 run() 返回，再交给事件循环回收。
        断开信号是为了让任何仍排在队列里的 output/finished 静默丢弃。
        """
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        try:
            worker.signals.output.disconnect(self._on_output)
            worker.signals.finished.disconnect(self._on_finished)
        except (RuntimeError, TypeError):
            pass  # 已断开
        worker.wait(5000)
        worker.deleteLater()

    def _on_finished(self, exit_code: int):
        """构建完成"""
        # worker 被 taskkill 之后仍会走到这里并带非零退出码。
        # 不判断 _cancelled 的话，"已取消" 会被覆写成 "构建失败 (code: 1)"。
        #
        # 另外要认准发信人：取消后立刻重新开始构建时，旧 worker 的 finished
        # 可能才刚被投递。此时 self.worker 已经是新的了，照着回收会把正在
        # 跑的构建搞失联 —— 所以对不上就直接丢弃这条迟到事件。
        sender = self.sender()
        if self.worker is not None and sender is not self.worker.signals:
            return

        self._reclaim_worker()

        if self._cancelled:
            return

        self._set_ui_running(False)

        if exit_code == 0:
            self._log("\n🎉 执行成功!", "#6a9955")
            self.status_label.setText("✅ 执行成功")
            self.status_label.setStyleSheet("color: #6a9955;")
            if self._is_build:
                self._list_artifacts()
        else:
            self._log(f"\n❌ 执行失败 (exit code: {exit_code})", "#f14c4c")
            self.status_label.setText(f"❌ 执行失败 (code: {exit_code})")
            self.status_label.setStyleSheet("color: #f14c4c;")

    def _open_release_dir(self):
        """打开输出目录（跨平台）"""
        rel = release_dir()
        rel.mkdir(exist_ok=True)

        try:
            if sys.platform == "win32":
                os.startfile(str(rel))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(rel)], check=False)
            else:  # Linux
                subprocess.run(["xdg-open", str(rel)], check=False)
        except Exception as e:
            QMessageBox.warning(self, "无法打开目录", f"请手动打开: {rel}\n\n错误: {e}")


# ============================================================================
# 入口
# ============================================================================


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 深色主题
    app.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #2d2d2d;
            color: #d4d4d4;
        }
        QGroupBox {
            font-weight: bold;
            border: 1px solid #3c3c3c;
            border-radius: 6px;
            margin-top: 12px;
            padding-top: 12px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 4px;
        }
        QComboBox, QLineEdit {
            background-color: #3c3c3c;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 6px;
            color: #d4d4d4;
        }
        QComboBox:hover, QLineEdit:focus {
            border-color: #0078d4;
        }
        /* 不要给 ::drop-down 写规则。一旦这个 subcontrol 被样式表接管，
           Qt 就不再画原生下拉箭头，而样式表里又没给 image —— 结果下拉框
           变成一个和 QLineEdit 一模一样的方框，完全看不出可以点开选择。 */
        QComboBox QAbstractItemView {
            background-color: #3c3c3c;
            color: #d4d4d4;
            border: 1px solid #555;
            selection-background-color: #0078d4;
            selection-color: white;
            outline: none;
        }
        QCheckBox {
            color: #d4d4d4;
        }
        QPushButton {
            background-color: #3c3c3c;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 6px 12px;
            color: #d4d4d4;
        }
        QPushButton:hover {
            background-color: #4a4a4a;
            border-color: #666;
        }
        QPushButton:pressed {
            background-color: #333;
        }
        QProgressBar {
            border: 1px solid #555;
            border-radius: 4px;
            background-color: #3c3c3c;
            text-align: center;
        }
        QProgressBar::chunk {
            background-color: #0078d4;
            border-radius: 3px;
        }
        QLabel {
            color: #d4d4d4;
        }
    """)

    window = BuildGUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
