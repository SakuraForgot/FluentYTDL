#!/usr/bin/env python3
"""
FluentYTDL 打包工具 GUI

基于 PySide6 的图形化构建界面，支持：
- 选择构建目标（全部、安装包、完整版、便携版）
- 实时显示构建日志
- 进度指示
- 一键执行常用任务

用法:
    python scripts/build_gui.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 确保可以导入项目模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QObject, QThread, Signal  # noqa: E402
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
# 工作线程
# ============================================================================


class BuildSignals(QObject):
    """构建信号"""

    output = Signal(str)
    finished = Signal(int)  # exit code
    progress = Signal(str)  # status message


class BuildWorker(QThread):
    """后台构建工作线程"""

    signals = BuildSignals()

    def __init__(self, command: list[str], cwd: Path | None = None):
        super().__init__()
        self.command = command
        self.cwd = cwd or ROOT
        self._process: subprocess.Popen | None = None

    def run(self):
        try:
            self._process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.cwd),
                bufsize=1,
            )

            if self._process.stdout:
                for line in iter(self._process.stdout.readline, ""):
                    if line:
                        self.signals.output.emit(line.rstrip())
                        # 检测进度关键词（更全面）
                        lower_line = line.lower()
                        progress_keywords = [
                            "构建",
                            "building",
                            "打包",
                            "packaging",
                            "编译",
                            "compiling",
                            "生成",
                            "generating",
                            "下载",
                            "downloading",
                            "提取",
                            "extracting",
                            "压缩",
                            "compressing",
                            "复制",
                            "copying",
                        ]
                        if any(kw in lower_line for kw in progress_keywords):
                            self.signals.progress.emit(line.strip()[:50])

            self._process.wait()
            self.signals.finished.emit(self._process.returncode or 0)

        except Exception as e:
            self.signals.output.emit(f"❌ 错误: {e}")
            self.signals.finished.emit(1)

    def terminate_process(self):
        if self._process:
            self._process.terminate()


# ============================================================================
# 主窗口
# ============================================================================


class BuildGUI(QMainWindow):
    """构建工具主窗口"""

    def __init__(self):
        super().__init__()
        self.worker: BuildWorker | None = None
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        self.setWindowTitle("FluentYTDL 打包工具")
        self.setMinimumSize(700, 550)

        # 尝试设置图标
        icon_path = ROOT / "assets" / "logo.ico"
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
        self.target_combo.addItems(
            [
                "全部 (All) - 免安装 7z 包 + Inno Setup 安装向导",
                "免安装包 (7z) - 仅生成基于 7z 的绿化纯净包",
                "安装向导 (Setup) - 仅生成 Inno Setup 安装包",
            ]
        )
        self.target_combo.setMinimumWidth(350)
        target_row.addWidget(self.target_combo)
        target_row.addStretch()
        target_layout.addLayout(target_row)

        # 打包配置选项
        options_row = QHBoxLayout()
        self.skip_hygiene_cb = QCheckBox("跳过环境污染体检 (--skip-hygiene)")
        self.skip_hygiene_cb.setToolTip(
            "开启后，即使环境中安装了黑名单依赖（如 torch, pandas）也将强行打包"
        )
        options_row.addWidget(self.skip_hygiene_cb)
        options_row.addStretch()
        target_layout.addLayout(options_row)

        # 版本号
        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("版本号:"))
        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText("留空自动从 pyproject.toml 读取")
        self.version_edit.setMaximumWidth(200)
        version_row.addWidget(self.version_edit)
        version_row.addStretch()
        target_layout.addLayout(version_row)

        layout.addWidget(target_group)

        # === 快捷操作区域 ===
        actions_group = QGroupBox("🔧 快捷操作")
        actions_layout = QHBoxLayout(actions_group)

        self.btn_fetch_tools = QPushButton("📥 下载工具")
        self.btn_fetch_tools.setToolTip("下载 yt-dlp, ffmpeg, deno")
        actions_layout.addWidget(self.btn_fetch_tools)

        self.btn_collect_licenses = QPushButton("📄 收集许可证")
        self.btn_collect_licenses.setToolTip("收集第三方许可证")
        actions_layout.addWidget(self.btn_collect_licenses)

        self.btn_gen_checksums = QPushButton("🔐 生成校验和")
        self.btn_gen_checksums.setToolTip("生成 SHA256SUMS.txt")
        actions_layout.addWidget(self.btn_gen_checksums)

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
        self.btn_fetch_tools.clicked.connect(lambda: self._run_script("fetch_tools.py"))
        self.btn_collect_licenses.clicked.connect(lambda: self._run_script("collect_licenses.py"))
        self.btn_gen_checksums.clicked.connect(lambda: self._run_script("checksums.py"))
        self.btn_open_release.clicked.connect(self._open_release_dir)

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
        self.version_edit.setEnabled(not running)
        self.btn_fetch_tools.setEnabled(not running)
        self.btn_collect_licenses.setEnabled(not running)
        self.btn_gen_checksums.setEnabled(not running)

    def _get_target(self) -> str:
        """获取选择的构建目标"""
        idx = self.target_combo.currentIndex()
        return ["all", "7z", "setup"][idx]

    def _start_build(self):
        """开始构建"""
        target = self._get_target()
        version = self.version_edit.text().strip()
        skip_hygiene = self.skip_hygiene_cb.isChecked()

        self.log_text.clear()
        self._log(f"🚀 开始执行新编排流水线: 输出 {target}", "#4ec9b0")
        if version:
            self._log(f"   覆盖版本号: {version}", "#808080")
        if skip_hygiene:
            self._log("   ! 警告: 已跳过无菌环境体检", "#cca700")
        self._log("")

        cmd = [sys.executable, str(ROOT / "scripts" / "build.py"), "--target", target]
        if version:
            cmd.extend(["--version", version])
        if skip_hygiene:
            cmd.append("--skip-hygiene")

        self._run_command(cmd)

    def _run_script(self, script_name: str):
        """运行指定脚本"""
        self.log_text.clear()
        self._log(f"🔧 运行: {script_name}", "#4ec9b0")
        self._log("")

        cmd = [sys.executable, str(ROOT / "scripts" / script_name)]
        self._run_command(cmd)

    def _run_command(self, cmd: list[str]):
        """运行命令"""
        self._set_ui_running(True)
        self.status_label.setText("正在执行...")

        self.worker = BuildWorker(cmd)
        self.worker.signals.output.connect(self._on_output)
        self.worker.signals.progress.connect(self._on_progress)
        self.worker.signals.finished.connect(self._on_finished)
        self.worker.start()

    def _cancel_build(self):
        """取消构建"""
        if self.worker:
            self._log("\n⏹ 用户取消构建", "#d7ba7d")
            self.worker.terminate_process()
            self.worker.quit()
            self.worker.wait()

            # 重置UI状态
            self._set_ui_running(False)
            self.status_label.setText("已取消")
            self.status_label.setStyleSheet("color: #cca700;")

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
        elif text.startswith("==="):
            self._log(text, "#c586c0")
        else:
            self._log(text)

    def _on_progress(self, text: str):
        """处理进度"""
        self.status_label.setText(text[:40] + "..." if len(text) > 40 else text)

    def _on_finished(self, exit_code: int):
        """构建完成"""
        self._set_ui_running(False)

        if exit_code == 0:
            self._log("\n🎉 构建成功!", "#6a9955")
            self.status_label.setText("✅ 构建成功")
            self.status_label.setStyleSheet("color: #6a9955;")
        else:
            self._log(f"\n❌ 构建失败 (exit code: {exit_code})", "#f14c4c")
            self.status_label.setText(f"❌ 构建失败 (code: {exit_code})")
            self.status_label.setStyleSheet("color: #f14c4c;")

    def _open_release_dir(self):
        """打开输出目录（跨平台）"""
        release_dir = ROOT / "release"
        release_dir.mkdir(exist_ok=True)

        try:
            if sys.platform == "win32":
                os.startfile(str(release_dir))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(release_dir)], check=False)
            else:  # Linux
                subprocess.run(["xdg-open", str(release_dir)], check=False)
        except Exception as e:
            QMessageBox.warning(self, "无法打开目录", f"请手动打开: {release_dir}\n\n错误: {e}")


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
        QComboBox::drop-down {
            border: none;
            width: 20px;
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
