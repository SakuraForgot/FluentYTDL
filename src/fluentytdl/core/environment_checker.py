"""
环境自检模块 - P0

负责在启动直播录制前检查：
1. FFmpeg 是否可用
2. GPU 加速编码器 (NVENC, QSV, AMF)
3. 磁盘写入权限
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fluentytdl.utils.logger import logger
from fluentytdl.utils.paths import find_bundled_executable, get_clean_env

if TYPE_CHECKING:
    pass


class EnvironmentChecker:
    """
    环境自检

    单例模式，检查录制所需的基础设施。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._ffmpeg_exe = None
        self._ffprobe_exe = None
        self._encoders = []
        self._initialized = True

    def check_all(self) -> dict:
        """运行所有检查"""
        results = {
            "ffmpeg": self.check_ffmpeg(),
            "ffprobe": self.check_ffprobe(),
            "gpu": self.check_gpu_encoders(),
        }
        return results

    def refresh(self) -> None:
        """强制刷新环境检测缓存"""
        self._ffmpeg_exe = None
        self._ffprobe_exe = None
        self._encoders = []
        self.check_all()

    def check_ffmpeg(self) -> bool:
        """检查 FFmpeg 是否可用，返回最优引用方式 (优先使用内置)。

        用 `find_bundled_executable()` 而不是自己拼 `sys.argv[0]` 的同级目录：自带工具
        是按组件分子目录放的（``bin/ffmpeg/ffmpeg.exe`` / dev 下 ``assets/bin/ffmpeg/``），
        而旧代码只找 ``bin/ffmpeg.exe`` 这一层，于是"内置优先"在两种模式下都从未命中，
        一路掉到系统 PATH —— 拿到的可能是缺编码器的 essentials 构建，而 yt-dlp 子进程
        用的却是自带的 full 构建。两边不一致比单纯找不到更难查。
        """
        import shutil

        # 1. 优先检查应用内置目录（自带 full 构建，编码器齐全）
        bundled = find_bundled_executable(
            "ffmpeg/ffmpeg.exe" if sys.platform == "win32" else "ffmpeg/ffmpeg",
            "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg",
        )
        if bundled is not None:
            self._ffmpeg_exe = str(bundled.absolute())
            logger.info(f"使用内置 FFmpeg: {self._ffmpeg_exe}")
            return True

        # 2. 最后检查 PATH (最通用且便携的方式)
        path = shutil.which("ffmpeg")
        if path:
            self._ffmpeg_exe = "ffmpeg"
            logger.info(f"使用系统 FFmpeg: {path}")
            return True

        return False

    def check_ffprobe(self) -> bool:
        """检查 FFprobe 是否可用（与 ffmpeg 同序：自带 → PATH）。"""
        bundled = find_bundled_executable(
            "ffmpeg/ffprobe.exe" if sys.platform == "win32" else "ffmpeg/ffprobe",
            "ffprobe.exe" if sys.platform == "win32" else "ffprobe",
        )
        if bundled is not None:
            self._ffprobe_exe = str(bundled.absolute())
            return True

        try:
            env = get_clean_env()
            subprocess.run(
                ["ffprobe", "-version"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                env=env,
            )
            self._ffprobe_exe = "ffprobe"
            return True
        except Exception:
            import shutil

            path = shutil.which("ffprobe")
            if path:
                self._ffprobe_exe = path
                return True
        return False

    def check_gpu_encoders(self) -> list[str]:
        """检测可用的 GPU 编码器"""
        if not self._ffmpeg_exe:
            return []

        encoders = []
        try:
            env = get_clean_env()
            result = subprocess.run(
                [self._ffmpeg_exe, "-encoders"],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                env=env,
            )
            output = result.stdout

            # NVIDIA NVENC
            if "h264_nvenc" in output:
                encoders.append("h264_nvenc")
            # Intel QSV
            if "h264_qsv" in output:
                encoders.append("h264_qsv")
            # AMD AMF
            if "h264_amf" in output:
                encoders.append("h264_amf")

            self._encoders = encoders
        except Exception as e:
            logger.error(f"检测 GPU 编码器失败: {e}")

        return encoders

    def get_best_encoder(self) -> str:
        """获取最佳编码器（优先 GPU）"""
        if not self._encoders:
            self.check_gpu_encoders()

        if "h264_nvenc" in self._encoders:
            return "h264_nvenc"
        if "h264_qsv" in self._encoders:
            return "h264_qsv"
        if "h264_amf" in self._encoders:
            return "h264_amf"

        return "libx264"  # 兜底 CPU

    def check_disk_permission(self, directory: str | Path) -> bool:
        """检查目录写入权限"""
        path = Path(directory)
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception:
                return False

        # 尝试写入临时文件
        test_file = path / ".write_test"
        try:
            test_file.write_text("test")
            test_file.unlink()
            return True
        except Exception:
            return False

    def get_ffmpeg_exe(self) -> str | None:
        return self._ffmpeg_exe

    def get_ffprobe_exe(self) -> str | None:
        return self._ffprobe_exe


# 导出单例获取函数
_checker = None


def get_environment_checker() -> EnvironmentChecker:
    global _checker
    if _checker is None:
        _checker = EnvironmentChecker()
    return _checker
