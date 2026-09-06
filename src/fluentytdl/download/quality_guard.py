from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

from ..core.config_manager import config_manager
from ..utils.logger import logger


@dataclass
class QualityIntent:
    """贯穿下载生命周期的质量目标记录。"""

    # 目标分辨率（如 1080, 720, None=最佳）
    target_height: int | None = None

    # 下载类型
    download_type: str = "video_audio"  # video_audio | video_only | audio_only

    # 选定的具体 format_id（如有）
    target_format_ids: list[str] | None = None

    # 预设标识
    preset_id: str | None = None  # best_mp4, 1080p, ...

    # 降级策略（warn 或 block）
    fallback_policy: str = "warn"

    # 来源路径标识（调试用）
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_height": self.target_height,
            "download_type": self.download_type,
            "target_format_ids": self.target_format_ids,
            "preset_id": self.preset_id,
            "fallback_policy": self.fallback_policy,
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityIntent:
        return cls(
            target_height=data.get("target_height"),
            download_type=data.get("download_type", "video_audio"),
            target_format_ids=data.get("target_format_ids"),
            preset_id=data.get("preset_id"),
            fallback_policy=data.get("fallback_policy", "warn"),
            source_path=data.get("source_path", ""),
        )


@dataclass
class QualityVerdict:
    """质量校验结果。"""

    passed: bool

    actual_height: int | None = None
    actual_format_id: str | None = None
    actual_vcodec: str | None = None

    deviation: str = ""
    deviation_severity: str = "none"  # "none" | "minor" | "major" | "critical"
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "actual_height": self.actual_height,
            "actual_format_id": self.actual_format_id,
            "actual_vcodec": self.actual_vcodec,
            "deviation": self.deviation,
            "deviation_severity": self.deviation_severity,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QualityVerdict:
        return cls(
            passed=data.get("passed", True),
            actual_height=data.get("actual_height"),
            actual_format_id=data.get("actual_format_id"),
            actual_vcodec=data.get("actual_vcodec"),
            deviation=data.get("deviation", ""),
            deviation_severity=data.get("deviation_severity", "none"),
            suggestion=data.get("suggestion", ""),
        )


class QualityGuard:
    """质量守卫引擎：提供格式生成预检与下载后核验。"""

    @staticmethod
    def preflight_quality_check(formats_list: list[dict], intent: QualityIntent) -> QualityVerdict:
        """预检质量：根据可用 formats_list 和 intent，判断是否能够满足目标。"""
        if intent.target_height is None or intent.download_type == "audio_only":
            return QualityVerdict(passed=True)

        available_heights = sorted(
            set(
                int(f.get("height") or 0)
                for f in formats_list
                if isinstance(f, dict)
                and str(f.get("vcodec") or "none") != "none"
                and f.get("height")
            ),
            reverse=True,
        )

        if not available_heights:
            # 无法获取分辨率列表，或仅有纯音频
            return QualityVerdict(passed=True)

        max_available = available_heights[0]
        if max_available >= intent.target_height:
            return QualityVerdict(passed=True)

        diff = intent.target_height - max_available
        severity = "major" if diff > 360 else "minor"

        return QualityVerdict(
            passed=False,
            actual_height=max_available,
            deviation=f"目标 {intent.target_height}p → 最高可用 {max_available}p",
            deviation_severity=severity,
            suggestion=f"该视频最高仅支持 {max_available}p，请确认是否继续下载。",
        )

    @staticmethod
    def post_verify(
        intent: QualityIntent,
        actual_height: int | None,
        actual_format_id: str | None,
        output_path: str = "",
    ) -> QualityVerdict:
        """下载后校验实际质量。"""
        if intent.target_height is None or intent.download_type == "audio_only":
            return QualityVerdict(
                passed=True, actual_height=actual_height, actual_format_id=actual_format_id
            )

        if actual_height is None or actual_height <= 0:
            # 如果从进度中未能提取出高度，且开启了精准验证，则用 ffprobe
            use_ffprobe = bool(config_manager.get("quality_guard_ffprobe", False))
            if use_ffprobe and output_path:
                probe_res = QualityGuard.verify_with_ffprobe(output_path)
                if probe_res:
                    actual_height = probe_res[1]

        if actual_height is None or actual_height <= 0:
            # 仍然无法获取实际高度，放行
            return QualityVerdict(
                passed=True, actual_height=actual_height, actual_format_id=actual_format_id
            )

        if actual_height >= intent.target_height:
            return QualityVerdict(
                passed=True, actual_height=actual_height, actual_format_id=actual_format_id
            )

        diff = intent.target_height - actual_height
        severity = "major" if diff > 360 else "minor"

        return QualityVerdict(
            passed=False,
            actual_height=actual_height,
            actual_format_id=actual_format_id,
            deviation=f"目标 {intent.target_height}p → 实际 {actual_height}p",
            deviation_severity=severity,
        )

    @staticmethod
    def verify_with_ffprobe(file_path: str) -> tuple[int, int] | None:
        """可选的 FFprobe 校验。返回 (width, height)"""
        try:
            from ..utils.path_utils import get_bin_path

            ffprobe_path = get_bin_path("ffprobe.exe")
            cmd = [
                ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=height,width",
                "-of",
                "json",
                file_path,
            ]
            # 这里是同步调用，不要在主线程中用
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            import json

            data = json.loads(proc.stdout)
            streams = data.get("streams", [])
            if streams:
                w = int(streams[0].get("width", 0))
                h = int(streams[0].get("height", 0))
                if w > 0 and h > 0:
                    return w, h
        except Exception as e:
            logger.debug(f"FFprobe check failed: {e}")
        return None


def resolve_format_with_guard(
    format_str: str,
    extra_opts: dict,
    *,
    formats_list: list[dict] | None,
    intent_max_height: int | None,
    intent_preset_id: str | None,
    download_type: str = "video_audio",
    source_path: str = "",
    trace: Any = None,
) -> tuple[str, dict, QualityIntent, QualityVerdict | None]:
    """统一格式解析 + 质量意图锚定 + 预检。

    Args:
        trace: 可选的 `FlowTrace` / `TaskTrace`。**必须显式传**：这个函数跑在 GUI 线程上，
            而 `current_flow()` 是 per-thread ContextVar、在 GUI 线程上恒为 None ——
            靠它取 flow 会让每条事件都变成 `flow=-` 的孤儿。缺省不传只是少了标识，
            事件照发（观测永远 best-effort）。
    """

    fallback_policy = str(config_manager.get("quality_guard_mode", "warn"))

    intent = QualityIntent(
        target_height=intent_max_height,
        download_type=download_type,
        preset_id=intent_preset_id,
        fallback_policy=fallback_policy,
        source_path=source_path,
    )

    # 解析 format_id 列表
    if "+" in format_str and not format_str.startswith("bv") and not format_str.startswith("best"):
        intent.target_format_ids = format_str.split("+")
    elif (
        not format_str.startswith("bv")
        and not format_str.startswith("best")
        and "+" not in format_str
    ):
        intent.target_format_ids = [format_str]

    verdict = None
    if formats_list and intent.target_height is not None:
        verdict = QualityGuard.preflight_quality_check(formats_list, intent)

    extra_opts["__fluentytdl_quality_intent"] = intent.to_dict()

    # ── 画质意图与预检裁决落日志 ──
    # 这两样以前只存在于内存和 UI 里：`QualityIntent` 被塞进 `extra_opts` 一路带到
    # worker，`QualityVerdict` 只在 `QualityGuardManager.on_quality_warning()` 连续
    # 三次超阈值时才写一行日志。于是"我明明选了 1080p 结果下到 720p"在日志里毫无痕迹。
    from ..observability import emit_event

    emit_event(
        "expect",
        trace=trace,
        stage="select",
        subsystem="quality_guard",
        fmt=format_str,
        **intent.to_dict(),
    )
    if verdict is not None:
        # 用 `decision` 而不是 `actual`：这是**预检**（拿可用 formats 对目标做的判断），
        # 不是下载完的事实。`kind=actual` 每个 run 只该有产物那一条，混进来就没法
        # 靠 `count(kind=actual)` 判断验证有没有跑过。
        emit_event(
            "decision",
            trace=trace,
            level="WARNING" if not verdict.passed else "INFO",
            stage="preflight",
            subsystem="quality_guard",
            passed=verdict.passed,
            target_height=intent.target_height,
            actual_height=verdict.actual_height,
            actual_format_id=verdict.actual_format_id,
            actual_vcodec=verdict.actual_vcodec,
            deviation=verdict.deviation or None,
            deviation_severity=verdict.deviation_severity,
            fallback_policy=fallback_policy,
        )

    return format_str, extra_opts, intent, verdict


class QualityGuardManager:
    """全局质量守卫管理器（单例），跟踪连续异常并决定是否暂停队列。"""

    def __init__(self):
        self._consecutive_failures = 0

    def on_quality_warning(self, verdict: QualityVerdict, worker_url: str):
        from ..download.download_manager import download_manager

        self._consecutive_failures += 1
        threshold = int(config_manager.get("quality_guard_suspend_threshold", 3))

        if self._consecutive_failures >= threshold:
            logger.warning(
                f"质量守卫：连续 {self._consecutive_failures} 个任务出现质量异常，触发风控防御机制"
            )

            suspended_count = download_manager.suspend_pending()

            if suspended_count > 0:
                # 触发消息中心通知 (后续实现)
                try:
                    from ..notification.notification_center import Notification, notification_center

                    notification_center.push(
                        Notification(
                            type="risk_control",
                            severity="critical",
                            title="队列已自动暂停",
                            message=f"连续 {self._consecutive_failures} 个任务出现质量降级异常，这可能意味着遭遇风控。已安全暂停排队中的 {suspended_count} 个任务以保护账号。",
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to push risk control notification: {e}")

            # 暂停后为了避免无休止触发，可以将计数器重置
            self._consecutive_failures = 0

    def on_quality_ok(self):
        self._consecutive_failures = 0


quality_guard_manager = QualityGuardManager()
