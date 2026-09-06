"""下载完整性保险丝（`executor` 关卡 2）的回归测试。

`ratio < 0.5` 这道保险丝原先形同虚设：预期总大小取的是 `max(各流)`，而 DASH 合并
产物必然大于任何单流，ratio 恒 > 1。改成按文件名分流累计后它才真正会触发。
"""

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.download.executor import DownloadExecutor  # noqa: E402
from fluentytdl.models.errors import YtDlpExecutionError  # noqa: E402


def _progress(filename: str, total: int, downloaded: int | None = None) -> bytes:
    """一条结构化进度行（字段顺序见 `executor` 的 --progress-template）。"""
    got = total if downloaded is None else downloaded
    return (f"FLUENTYTDL|download|{got}|{total}|NA|1048576|0|avc1|mp4a|mp4|{filename}").encode()


def _run(
    tmp_path: Path,
    lines: list[bytes],
    rc: int,
    extra_opts: dict | None = None,
) -> tuple[str | None, Exception | None]:
    process = MagicMock()
    process.stdout = BytesIO(b"\n".join(lines))
    process.wait.return_value = rc
    process.returncode = rc

    opts: dict = {"format": "bv+ba", "paths": {"home": str(tmp_path)}}
    opts.update(extra_opts or {})

    with (
        patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe")),
        patch("fluentytdl.download.executor.subprocess.Popen", return_value=process),
    ):
        try:
            return (
                DownloadExecutor().execute(
                    url="https://youtube.com/watch?v=mock_id_456",
                    ydl_opts=opts,
                    on_progress=lambda _e: None,
                    on_status=lambda _s: None,
                    on_path=lambda _p: None,
                    cancel_check=lambda: False,
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - 测试要区分抛没抛
            return None, exc


def test_truncated_merge_is_rejected(tmp_path):
    """视频 1.0 MB + 音频 0.1 MB，产物只有 0.5 MB → 45%，判定为不完整。

    这正是 `max()` 会漏掉的那一档：按 `max` 算预期是 1.0 MB，ratio 恰好 0.5，
    不小于阈值，于是一个残缺文件会被当成"退出码非零但产物有效"放过去。
    """
    merged = tmp_path / "video.mp4"
    merged.write_bytes(b"\0" * 500_000)

    _result, exc = _run(
        tmp_path,
        [
            _progress(str(tmp_path / "video.f315.mp4"), 1_000_000),
            _progress(str(tmp_path / "video.f251.m4a"), 100_000),
            f'[Merger] Merging formats into "{merged}"'.encode(),
            b"ERROR: unable to write data: [Errno 28] No space left on device",
        ],
        rc=1,
    )

    assert isinstance(exc, YtDlpExecutionError)
    assert exc.exit_code == 1


def test_complete_merge_is_accepted(tmp_path):
    """同样两条流，产物 1.05 MB → 95%，容器开销范围内，正常放过。"""
    merged = tmp_path / "video.mp4"
    merged.write_bytes(b"\0" * 1_050_000)

    result, exc = _run(
        tmp_path,
        [
            _progress(str(tmp_path / "video.f315.mp4"), 1_000_000),
            _progress(str(tmp_path / "video.f251.m4a"), 100_000),
            f'[Merger] Merging formats into "{merged}"'.encode(),
            b"ERROR: Unable to delete file video.f315.mp4.part-Frag1",
        ],
        rc=1,
    )

    assert exc is None
    assert result is not None and result.endswith("video.mp4")


def test_auxiliary_files_excluded_from_expected_size(tmp_path):
    """封面/字幕不进预期值：`actual_size` 那侧只量主媒体文件，口径必须一致。

    这里给封面一个夸张的 2 MB —— 若把它算进预期，ratio 会掉到 32% 被误杀。
    """
    merged = tmp_path / "video.mp4"
    merged.write_bytes(b"\0" * 950_000)

    result, exc = _run(
        tmp_path,
        [
            _progress(str(tmp_path / "video.f315.mp4"), 1_000_000),
            _progress(str(tmp_path / "video.jpg"), 2_000_000),
            _progress(str(tmp_path / "video.zh-Hans.vtt"), 2_000_000),
            f'[Merger] Merging formats into "{merged}"'.encode(),
            b"ERROR: Unable to delete file video.f315.mp4.part-Frag1",
        ],
        rc=1,
    )

    assert exc is None
    assert result is not None and result.endswith("video.mp4")


def test_no_progress_lines_skips_the_fuse(tmp_path):
    """`--download-sections` 走 FFmpegFD，完全没有进度行 —— 预期为 0，保险丝不参与判定。"""
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"\0" * 20_000)

    result, exc = _run(
        tmp_path,
        [
            f"[download] Destination: {out}".encode(),
            b"ERROR: Unable to delete file clip.mp4.part",
        ],
        rc=1,
    )

    assert exc is None
    assert result is not None and result.endswith("clip.mp4")


def test_audio_extraction_disables_the_fuse(tmp_path):
    """转成 mp3 之后产物本就该远小于原始流之和 —— 体积比对在这里没有意义。

    保险丝只在 rc != 0 的宽恕路径上被问到：宁可放过，也不能把一个正常的音频
    提取任务判成"不完整下载"。
    """
    out = tmp_path / "audio.mp3"
    out.write_bytes(b"\0" * 120_000)  # 仅为源流的 12%

    result, exc = _run(
        tmp_path,
        [
            _progress(str(tmp_path / "audio.f251.webm"), 1_000_000),
            f"[download] Destination: {out}".encode(),
            b"ERROR: Unable to delete file audio.f251.webm.part",
        ],
        rc=1,
        extra_opts={"extract_audio": True, "audio_format": "mp3"},
    )

    assert exc is None
    assert result is not None
