"""Tests for the standalone updater module (no Qt/network dependencies)."""

import importlib.util
import os
import sys
import zipfile
from pathlib import Path

import pytest

# Load updater.py directly via importlib to avoid triggering
# fluentytdl.core.__init__ -> config_manager -> PySide6 import chain.
_updater_path = (
    Path(__file__).resolve().parent.parent / "src" / "fluentytdl" / "core" / "updater.py"
)
_spec = importlib.util.spec_from_file_location("_updater_under_test", _updater_path)
_updater_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_updater_mod)

_move_extracted_files = _updater_mod._move_extracted_files
_verify_extraction = _updater_mod._verify_extraction
decide_watch_outcome = _updater_mod.decide_watch_outcome
extract_archive = _updater_mod.extract_archive
pick_launch_rung = _updater_mod.pick_launch_rung
request_admin_if_needed = _updater_mod.request_admin_if_needed
self_delete = _updater_mod.self_delete
wait_for_process = _updater_mod.wait_for_process

PROTECTED_NAMES = _updater_mod.PROTECTED_NAMES
READY_TIMEOUT = _updater_mod.READY_TIMEOUT
SURVIVAL_GRACE = _updater_mod.SURVIVAL_GRACE


class TestWaitForProcess:
    def test_nonexistent_pid_returns_true(self):
        """A PID that doesn't exist should return True immediately."""
        result = wait_for_process(99999, timeout=2)
        assert result is True

    def test_current_pid_times_out(self):
        """The current process is alive, so waiting should time out."""
        result = wait_for_process(os.getpid(), timeout=1)
        assert result is False


class TestExtractArchive:
    def test_zip_extraction(self, tmp_path):
        """Extracting a valid zip should produce the expected file."""
        # Create a zip with a test file
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("hello.txt", "world")

        dest = tmp_path / "out"
        dest.mkdir()
        extract_archive(zip_path, dest)
        assert (dest / "hello.txt").read_text() == "world"

    def test_unsupported_format_raises(self, tmp_path):
        """A .txt file should raise ValueError."""
        txt_path = tmp_path / "file.txt"
        txt_path.write_text("not an archive")
        with pytest.raises(ValueError, match="不支持的归档格式"):
            extract_archive(txt_path, tmp_path / "out")

    def test_7z_extraction_with_py7zr(self, tmp_path):
        """If py7zr is available, test 7z extraction."""
        py7zr = pytest.importorskip("py7zr")

        archive_path = tmp_path / "test.7z"
        with py7zr.SevenZipFile(archive_path, "w") as zf:
            zf.writestr(b"hello 7z", "hello.txt")

        dest = tmp_path / "out"
        dest.mkdir()
        extract_archive(archive_path, dest)
        assert (dest / "hello.txt").exists()


class TestRequestAdminIfNeeded:
    def test_user_directory_no_elevation(self, tmp_path):
        """A non-Program-Files directory should not request elevation."""
        result = request_admin_if_needed(tmp_path)
        assert result is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_local_appdata_no_elevation(self):
        """LOCALAPPDATA directory should not request elevation."""
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if local.exists():
            result = request_admin_if_needed(local / "FluentYTDL")
            assert result is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_already_elevated_sentinel_short_circuits(self):
        """`--elevated` 哨兵必须在任何路径判断之前就止步。

        这是"Program Files 用户永远收不到更新"的直接成因：IsUserAnAdmin 在某些
        环境下说谎（组策略 / 容器 / 被 hook），于是提权后的实例再次提权，
        每一轮都弹 UAC、都拉起一个新 updater，永远走不到替换那一步。
        """
        program_files = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
        result = request_admin_if_needed(
            program_files / "FluentYTDL",
            already_elevated=True,
            # 故意让权限查询说"不是管理员"：哨兵优先级必须高于它。
            is_admin_fn=lambda: False,
        )
        assert result is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_admin_process_needs_no_elevation(self):
        """已经是管理员 → 不提权（即使目标在 Program Files）。"""
        program_files = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
        result = request_admin_if_needed(
            program_files / "FluentYTDL",
            is_admin_fn=lambda: True,
        )
        assert result is False


class TestSelfDelete:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_self_delete_creates_process(self, tmp_path):
        """self_delete should spawn a cmd process without raising."""
        fake_exe = tmp_path / "fake.exe"
        fake_exe.write_bytes(b"MZ")
        # Should not raise
        self_delete(fake_exe)


class TestVerifyExtraction:
    def test_valid_extraction_passes(self, tmp_path):
        """A complete extraction (exe + base_library.zip + python3*.dll) should pass."""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "base_library.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        (internal / "python311.dll").write_bytes(b"dll")
        (tmp_path / "VERSION").write_text("v-3.0.18")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is True

    def test_missing_exe_fails(self, tmp_path):
        """Missing exe should fail verification."""
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "lib.dll").write_bytes(b"dll")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_empty_exe_fails(self, tmp_path):
        """Zero-byte exe should fail verification."""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"")
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "lib.dll").write_bytes(b"dll")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_empty_internal_fails(self, tmp_path):
        """Empty _internal/ should fail verification."""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        (tmp_path / "_internal").mkdir()

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_missing_internal_fails(self, tmp_path):
        """Missing _internal/ should fail verification."""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_missing_base_library_fails(self, tmp_path):
        """缺 base_library.zip → bootloader 装不起 stdlib，属于必然启动失败的归档。"""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "python311.dll").write_bytes(b"dll")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_zero_byte_base_library_fails(self, tmp_path):
        """0 字节的 base_library.zip 与缺失等价（截断的归档）。"""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "base_library.zip").write_bytes(b"")
        (internal / "python311.dll").write_bytes(b"dll")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_missing_python_dll_fails(self, tmp_path):
        """缺 python3*.dll → 根本没有解释器。"""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "base_library.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        (internal / "some_other.dll").write_bytes(b"dll")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_zero_byte_python_dll_fails(self, tmp_path):
        """0 字节的 python3*.dll 不算"找到了解释器"。"""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "base_library.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        (internal / "python313.dll").write_bytes(b"")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is False

    def test_missing_version_file_is_non_fatal(self, tmp_path):
        """VERSION 缺失只警告 —— 它不影响新版能否启动。"""
        (tmp_path / "FluentYTDL.exe").write_bytes(b"MZ" + b"\x00" * 100)
        internal = tmp_path / "_internal"
        internal.mkdir()
        (internal / "base_library.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        (internal / "python313.dll").write_bytes(b"dll")

        assert _verify_extraction(tmp_path, "FluentYTDL.exe") is True


class TestMoveExtractedFiles:
    def test_move_all_files(self, tmp_path):
        """All files from tmp_dir should be moved to dest_dir."""
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()

        (src / "FluentYTDL.exe").write_bytes(b"MZ")
        (src / "VERSION").write_text("v-3.0.18")
        docs = src / "docs"
        docs.mkdir()
        (docs / "README.md").write_text("hello")

        assert _move_extracted_files(src, dest) is True

        assert (dest / "FluentYTDL.exe").exists()
        assert (dest / "VERSION").read_text() == "v-3.0.18"
        assert (dest / "docs" / "README.md").read_text() == "hello"
        # Source should be empty after move
        assert not any(src.iterdir())

    def test_move_overwrites_existing(self, tmp_path):
        """Existing files in dest should be overwritten."""
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()

        (src / "VERSION").write_text("v-3.0.19")
        (dest / "VERSION").write_text("v-3.0.18")  # old version

        assert _move_extracted_files(src, dest) is True
        assert (dest / "VERSION").read_text() == "v-3.0.19"

    def test_protected_names_are_never_overwritten(self, tmp_path):
        """脏归档里的用户数据条目一律跳过 —— 覆盖它们等于销毁用户数据。"""
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()

        (src / "config.json").write_text('{"from": "archive"}')
        (src / "FluentYTDL.exe").write_bytes(b"MZ")
        state = src / "state"
        state.mkdir()
        (state / "tasks.db").write_bytes(b"archive db")

        (dest / "config.json").write_text('{"from": "user"}')
        dest_state = dest / "state"
        dest_state.mkdir()
        (dest_state / "tasks.db").write_bytes(b"user db")

        assert _move_extracted_files(src, dest) is True

        assert (dest / "config.json").read_text() == '{"from": "user"}'
        assert (dest_state / "tasks.db").read_bytes() == b"user db"
        assert (dest / "FluentYTDL.exe").exists()
        # 被跳过的条目仍留在临时目录（调用方会整个删掉它）
        assert (src / "config.json").exists()

    def test_protected_names_match_case_insensitively(self, tmp_path):
        """NTFS 大小写不敏感，而脏归档里的大小写不可控。

        精确匹配会被 `Config.json` / `STATE` 绕过 —— 那正好是"更新之后我的设置
        和任务列表变回了开发者的"这条投诉。
        """
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()

        (src / "Config.json").write_text('{"from": "archive"}')
        (src / "Logs").mkdir()
        (src / "Logs" / "app.log").write_text("archive log")
        (src / "Portable.TXT").write_text("archive marker")
        (src / ".Migrated_V2").write_text("archive marker")

        (dest / "config.json").write_text('{"from": "user"}')

        assert _move_extracted_files(src, dest) is True

        assert (dest / "config.json").read_text() == '{"from": "user"}'
        assert not (dest / "Logs").exists()
        assert not (dest / "Portable.TXT").exists()
        assert not (dest / ".Migrated_V2").exists()

    def test_every_protected_name_is_skipped(self, tmp_path):
        """整张 PROTECTED_NAMES 表逐条过一遍，防止新增条目忘了生效。"""
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()

        for name in PROTECTED_NAMES:
            (src / name).write_text("from archive")

        assert _move_extracted_files(src, dest) is True
        assert not any(dest.iterdir())


class TestPickLaunchRung:
    """三级降权阶梯的选级逻辑（纯函数）。"""

    def test_shell_token_preferred(self):
        assert pick_launch_rung("S-1-5-21-1", "S-1-5-21-1", "S-1-5-18", "S-1-5-21-1") == 1

    def test_falls_to_linked_token(self):
        """Explorer 没跑 → shell token 拿不到，退到 linked token。"""
        assert pick_launch_rung(None, "S-1-5-21-1", "S-1-5-18", "S-1-5-21-1") == 2

    def test_falls_to_shell_execute(self):
        """前两级都拿不到，但自身身份就是原用户（未提权场景）→ 退化路径。"""
        assert pick_launch_rung(None, None, "S-1-5-21-1", "S-1-5-21-1") == 3

    def test_ots_elevation_returns_none(self):
        """OTS 提权：三级令牌没有一个属于发起更新的用户 → 不自动启动。

        宁可让用户多点一次桌面图标，也不能用 Bob 的身份去开 Alice 的数据目录。
        """
        assert (
            pick_launch_rung("S-1-5-21-BOB", "S-1-5-21-BOB", "S-1-5-21-BOB", "S-1-5-21-ALICE")
            is None
        )

    def test_shell_sid_mismatch_skips_to_matching_rung(self):
        """shell token 存在但身份不对 → 不能用它，往下找身份对得上的那一级。"""
        assert pick_launch_rung("S-1-5-21-BOB", "S-1-5-21-ALICE", None, "S-1-5-21-ALICE") == 2

    def test_no_origin_sid_falls_back_to_integrity_only(self):
        """旧主程序不传 --origin-user-sid → 退回"哪一级拿得到就用哪一级"。

        这条兼容分支不能删：旧版主程序拉起新 updater 时不知道该传 SID，
        按身份校验一刀切会让它永远走 rung=None、永远不自动启动新版。
        """
        assert pick_launch_rung("S-1-5-21-1", None, None, "") == 1
        assert pick_launch_rung(None, "S-1-5-21-1", None, "") == 2
        assert pick_launch_rung(None, None, None, "") == 3


class TestDecideWatchOutcome:
    """看门狗判定（纯函数）。两套协议必须是两套判定。"""

    def test_ready_pid_and_token_match_commits(self):
        payload = {"pid": 4242, "token": "nonce-abc"}
        assert decide_watch_outcome("ready", payload, 4242, "nonce-abc", True, 1.0) == "commit"

    def test_ready_pid_mismatch_rolls_back(self):
        """PID 不匹配 → 收到的是别人的 READY（PID 重用 / 陈旧文件）。"""
        payload = {"pid": 1111, "token": "nonce-abc"}
        assert decide_watch_outcome("ready", payload, 4242, "nonce-abc", True, 1.0) == "rollback"

    def test_ready_token_mismatch_rolls_back(self):
        """nonce 不匹配 → 上一轮更新留下的陈旧 .update_ready。"""
        payload = {"pid": 4242, "token": "stale-nonce"}
        assert decide_watch_outcome("ready", payload, 4242, "nonce-abc", True, 1.0) == "rollback"

    def test_ready_still_waiting(self):
        """没有 READY、进程还活着、没到超时 → 继续等。"""
        assert decide_watch_outcome("ready", None, 4242, "nonce-abc", True, 5.0) == "wait"

    def test_ready_process_died_rolls_back(self):
        assert decide_watch_outcome("ready", None, 4242, "nonce-abc", False, 5.0) == "rollback"

    def test_ready_timeout_rolls_back(self):
        assert (
            decide_watch_outcome("ready", None, 4242, "nonce-abc", True, READY_TIMEOUT + 1)
            == "rollback"
        )

    def test_survival_grace_reached_commits(self):
        """弱监护：活过宽限期即认定启动成功。"""
        assert (
            decide_watch_outcome("survival", None, 4242, "", True, SURVIVAL_GRACE + 0.1) == "commit"
        )

    def test_survival_ignores_ready_timeout(self):
        """survival 模式**没有** READY 通道，绝不能因为"收不到 READY"而超时回滚。

        这正是旧主程序拉起新 updater 的场景：一个完全健康的新版在 90 秒后被判
        超时、TerminateProcess、回滚 —— 纯粹自造的故障。
        """
        assert (
            decide_watch_outcome("survival", None, 4242, "", True, READY_TIMEOUT * 10) == "commit"
        )

    def test_survival_early_crash_rolls_back(self):
        """宽限期内就死掉 = "起来就崩"，这是 survival 唯一抓得到的故障。"""
        assert decide_watch_outcome("survival", None, 4242, "", False, 1.0) == "rollback"

    def test_survival_still_waiting(self):
        assert decide_watch_outcome("survival", None, 4242, "", True, 1.0) == "wait"

    def test_unknown_mode_treated_as_survival(self):
        """未知 watch_mode 按 survival 处理 —— 宁可漏判也绝不误杀。"""
        assert decide_watch_outcome("", None, 4242, "", True, SURVIVAL_GRACE + 1) == "commit"
        assert decide_watch_outcome("bogus", None, 4242, "", True, 1.0) == "wait"
