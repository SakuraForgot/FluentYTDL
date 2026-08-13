"""数据目录双轨解析 + 逐项合并迁移的测试。

这一整套的存在理由是一条真实投诉：**"更新之后我的设置和任务列表全没了。"**
成因是旧 `user_data_dir()` 用 `.writetest` 写探针决定数据落点 —— 提权会话
（updater 曾让新版继承管理员令牌）写得进 ``C:\\Program Files\\FluentYTDL``，
普通会话写不进只能退回 ``~/Documents\\FluentYTDL``，同一台机器的数据分裂成两棵树。

所以这里断言两件事：
  1. 落点由**声明**（环境变量 / portable.txt / frozen）决定，绝不由写探测决定；
  2. 两棵树都有真数据时逐项裁决、只复制不删源，且**重跑必须幂等**
     —— 迁移标记故意延迟到 READY 才写，这条路径一定会被反复走。

按路径直接加载 paths.py（它不 import 任何 fluentytdl 内部模块），避开
fluentytdl.utils.__init__ 的导入链。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATHS_FILE = Path(__file__).resolve().parent.parent / "src" / "fluentytdl" / "utils" / "paths.py"

#: 固定基准时间。迁移的胜负判定容差是 2 秒（`_MTIME_TOLERANCE`），所以测试里
#: 任何"谁更新"的差距都必须显著大于它 —— 用 "现在" 建出来的文件全都会打平。
T0 = 1_700_000_000.0


@pytest.fixture
def paths_mod():
    """每个测试一份全新的模块实例。

    迁移报告（`_MIGRATION_LOG` / `_MIGRATION_FAILURES` / `_MIGRATION_CONFLICTS`）
    与 `_MIGRATION_OK` 都是模块级全局，共用实例会让测试互相污染。
    """
    spec = importlib.util.spec_from_file_location("_paths_under_test", _PATHS_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(paths_mod, tmp_path, monkeypatch):
    """三个候选根就位：目的地 / ~/Documents / 安装目录。

    三者都在同一个 tmp_path 卷下 —— `_install_item()` 用 `os.replace` 落位，
    跨卷会直接抛 OSError。真实场景里暂存目录就在目的地内部，同样保证同卷。
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    documents = tmp_path / "documents"
    install = tmp_path / "install"

    monkeypatch.setattr(paths_mod, "user_data_dir", lambda app_name="FluentYTDL": dest)
    monkeypatch.setattr(paths_mod, "old_user_data_dir", lambda app_name="FluentYTDL": documents)
    monkeypatch.setattr(paths_mod, "frozen_app_dir", lambda: install)

    return SimpleNamespace(mod=paths_mod, dest=dest, documents=documents, install=install)


def write_at(path: Path, text: str, mtime: float) -> Path:
    """写文件并把 mtime 钉死 —— 判定靠 mtime，不能让测试依赖执行速度。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def make_state(root: Path, mtime: float, logical: float | None = None) -> Path:
    """造一个 `state/` 目录。给了 logical 就建真 sqlite 库（含 tasks 表）。"""
    state = root / "state"
    tasks_dir = state / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    if logical is None:
        write_at(state / "marker.txt", f"state @ {mtime}", mtime)
        return state

    db = tasks_dir / "tasks.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO tasks VALUES ('t1', ?)", (logical,))
        conn.commit()
    finally:
        conn.close()
    os.utime(db, (mtime, mtime))
    return state


def make_wal_state(
    root: Path, mtime: float, committed: float, wal_only: float
) -> tuple[Path, sqlite3.Connection]:
    """造一个 WAL 模式的 `state/`，**最新事务只躺在 `-wal` 里**。

    这是"用户刚关掉程序（或程序崩了）"的真实磁盘形态：主库文件里只有旧事务，
    最新那批任务还在 WAL 中等待 checkpoint。

    连接必须**保持打开**才能让 `-wal` 留在盘上 —— SQLite 在最后一个连接关闭时
    会自动 checkpoint 并删掉 `-wal`/`-shm`。所以连接一并返回，由调用方关闭。
    """
    state = root / "state"
    tasks = state / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    db = tasks / "tasks.db"

    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, updated_at REAL NOT NULL)")
    conn.execute("INSERT INTO tasks VALUES ('committed', ?)", (committed,))
    conn.commit()
    # 切 WAL 之后的这一笔就不再落主库了；关掉自动 checkpoint 是双保险。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO tasks VALUES ('in_wal', ?)", (wal_only,))
    conn.commit()

    wal = db.with_name(db.name + "-wal")
    assert wal.exists(), "构造失败：-wal 不存在，最新事务并没有留在 WAL 里"
    # 主库与 -wal 的 mtime 一起钉旧 —— 否则物理时间会替逻辑时间赢下这一局，
    # 断言就测不出"WAL 里的事务被看见了"。
    for path in (db, wal):
        os.utime(path, (mtime, mtime))
    return state, conn


def walk_set(root: Path) -> set[str]:
    """root 下所有目录与文件的相对路径集合（幂等断言的对象）。"""
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            out.add((base / name).relative_to(root).as_posix())
    return out


def _code_constants(fn) -> str:
    """把函数（含嵌套函数）的常量池拼成一个字符串，用于代码级断言。

    **跳过 docstring** —— 文档里必须能继续解释 `.writetest` 那段历史，
    断言要盯的是真正会被执行的字面量。
    """
    docs = {fn.__doc__}
    chunks: list[str] = []

    def visit(code) -> None:
        for const in code.co_consts:
            if isinstance(const, str):
                if const not in docs:
                    chunks.append(const)
            elif hasattr(const, "co_consts"):
                first = const.co_consts[0] if const.co_consts else None
                if isinstance(first, str):
                    docs.add(first)
                visit(const)

    visit(fn.__code__)
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# user_data_dir()：双轨解析
# ---------------------------------------------------------------------------


class TestUserDataDirDoubleTrack:
    def test_not_frozen_uses_project_root(self, paths_mod, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        monkeypatch.delenv(paths_mod.DATA_DIR_ENV, raising=False)
        monkeypatch.setattr(paths_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(paths_mod, "project_root", lambda: proj)

        assert paths_mod.user_data_dir() == proj
        assert proj.is_dir()  # 必须自己建出来：yt-dlp 子进程拿它当 CWD

    def test_frozen_with_portable_marker_uses_exe_dir(self, paths_mod, tmp_path, monkeypatch):
        exe_dir = tmp_path / "portable_app"
        exe_dir.mkdir()
        (exe_dir / paths_mod.PORTABLE_MARKER).write_text("marker", encoding="utf-8")
        monkeypatch.delenv(paths_mod.DATA_DIR_ENV, raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
        monkeypatch.setattr(paths_mod, "frozen_app_dir", lambda: exe_dir)

        assert paths_mod.user_data_dir() == exe_dir

    def test_frozen_without_marker_uses_local_appdata(self, paths_mod, tmp_path, monkeypatch):
        exe_dir = tmp_path / "installed_app"
        exe_dir.mkdir()
        local = tmp_path / "localappdata"
        monkeypatch.delenv(paths_mod.DATA_DIR_ENV, raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
        monkeypatch.setattr(paths_mod, "frozen_app_dir", lambda: exe_dir)

        assert paths_mod.user_data_dir() == local / "FluentYTDL"

    def test_override_outranks_marker_and_local_appdata(self, paths_mod, tmp_path, monkeypatch):
        """`--data-dir` 是最高优先级 —— updater 降权启动新版时就靠它。

        新版的数据落点必须完全不依赖"被继承的环境"，否则提权链上任何一环出错
        都会让数据落到另一棵树里。
        """
        override = tmp_path / "explicit"
        exe_dir = tmp_path / "portable_app"
        exe_dir.mkdir()
        (exe_dir / paths_mod.PORTABLE_MARKER).write_text("marker", encoding="utf-8")
        monkeypatch.setenv(paths_mod.DATA_DIR_ENV, str(override))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
        monkeypatch.setattr(paths_mod, "frozen_app_dir", lambda: exe_dir)

        assert paths_mod.user_data_dir() == override

    def test_blank_override_is_ignored(self, paths_mod, tmp_path, monkeypatch):
        """空串 / 纯空白的环境变量不算覆盖（子进程继承到空值是常态）。"""
        proj = tmp_path / "proj"
        monkeypatch.setenv(paths_mod.DATA_DIR_ENV, "   ")
        monkeypatch.setattr(paths_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(paths_mod, "project_root", lambda: proj)

        assert paths_mod.user_data_dir() == proj

    def test_no_write_probe_artifact(self, paths_mod, tmp_path, monkeypatch):
        """解析落点绝不能留下探针文件，也绝不能靠探针决定落点。

        `.writetest` 就是数据分裂的直接成因：提权会话探测成功 → 写进
        Program Files，普通会话探测失败 → 退回 ~/Documents。
        """
        proj = tmp_path / "proj"
        monkeypatch.delenv(paths_mod.DATA_DIR_ENV, raising=False)
        monkeypatch.setattr(paths_mod, "is_frozen", lambda: False)
        monkeypatch.setattr(paths_mod, "project_root", lambda: proj)

        target = paths_mod.user_data_dir()
        assert list(target.iterdir()) == []
        # 代码级回归闸门：探针名不许出现在这个函数的常量池里。
        # （只看 co_consts 而不是全文 —— 文件注释里必须能继续解释这段历史。）
        assert "writetest" not in _code_constants(paths_mod.user_data_dir)


# ---------------------------------------------------------------------------
# 迁移的边界条件
# ---------------------------------------------------------------------------


class TestMigrationNoOps:
    def test_no_legacy_data_marks_success(self, env):
        """全新安装：没有任何遗留数据。

        必须**标记成功**，否则每次启动都要重扫两个根 —— 而且 `.migrated_v2`
        永远写不出来，等于把这段扫描变成永久开销。
        """
        assert env.mod.migrate_user_data() is False
        assert env.mod._MIGRATION_OK is True
        assert env.mod.commit_migration_marker() is True
        assert (env.dest / env.mod.MIGRATION_MARKER).exists()

    def test_existing_marker_short_circuits(self, env):
        """标记已在 → 一步都不做，连遗留数据都不看。"""
        (env.dest / env.mod.MIGRATION_MARKER).write_text("done", encoding="utf-8")
        write_at(env.documents / "config.json", '{"who": "documents"}', T0)

        assert env.mod.migrate_user_data() is False
        assert not (env.dest / "config.json").exists()
        # None = 本次启动没跑过迁移，commit 无事可做
        assert env.mod._MIGRATION_OK is None
        assert env.mod.commit_migration_marker() is False

    def test_legacy_root_without_migration_items_is_ignored(self, env):
        """遗留根存在但里面没有任何参与迁移的项 → 视为无遗留数据。"""
        env.documents.mkdir(parents=True, exist_ok=True)
        (env.documents / "downloads").mkdir()
        (env.documents / "downloads" / "video.mp4").write_bytes(b"not our business")

        assert env.mod.migrate_user_data() is False
        assert env.mod._MIGRATION_OK is True
        assert not (env.documents / env.mod.MIGRATION_BREADCRUMB).exists()


class TestMigrationFirstRun:
    def test_single_root_copies_everything_and_keeps_sources(self, env):
        """首次迁移的正常路径：搬过来、源一个不动。

        **绝不删源**是硬规则：二进制回滚（updater Step 7）发生在新版 READY 之前，
        而迁移也在 READY 之前。删了源，回滚后的旧版去旧路径只会看到空目录。
        """
        write_at(env.documents / "config.json", '{"who": "documents"}', T0)
        write_at(env.documents / "logs" / "app.log", "documents log", T0)
        write_at(env.documents / "update_manifest_cache.json", "{}", T0)
        make_state(env.documents, T0)

        assert env.mod.migrate_user_data() is True

        assert (env.dest / "config.json").read_text(encoding="utf-8") == '{"who": "documents"}'
        assert (env.dest / "logs" / "app.log").read_text(encoding="utf-8") == "documents log"
        assert (env.dest / "update_manifest_cache.json").exists()
        assert (env.dest / "state" / "marker.txt").exists()

        # 源仍然完好
        assert (env.documents / "config.json").exists()
        assert (env.documents / "state" / "marker.txt").exists()

        # 迁移自己**绝不写**标记 —— 那是 finalize_startup() 的活
        assert not (env.dest / env.mod.MIGRATION_MARKER).exists()
        assert env.mod._MIGRATION_OK is True
        # 面包屑留在旧位置，告诉用户新数据在哪
        crumb = env.documents / env.mod.MIGRATION_BREADCRUMB
        assert crumb.read_text(encoding="utf-8").strip() == str(env.dest)
        # 暂存目录不留痕
        assert not (env.dest / env.mod.MIGRATE_TMP_DIRNAME).exists()

        _log, failures, conflicts = env.mod.take_migration_report()
        assert failures == []
        assert conflicts == []

    def test_non_kept_item_is_replaced_without_a_conflict_copy(self, env):
        """只有 `config.json` / `state` 会保留落选副本。

        缓存类文件（manifest 缓存、error_rules 覆盖）是可再生的，为它们在用户磁盘
        上留 `legacy_conflict_*` 只是噪音。
        """
        write_at(env.dest / "update_manifest_cache.json", '{"who": "dest"}', T0)
        write_at(env.documents / "update_manifest_cache.json", '{"who": "documents"}', T0 + 100)

        assert env.mod.migrate_user_data() is True

        target = env.dest / "update_manifest_cache.json"
        assert target.read_text(encoding="utf-8") == '{"who": "documents"}'
        assert not (env.dest / "legacy_conflict_dest").exists()
        assert env.mod._MIGRATION_CONFLICTS == []


class TestMigrationAdjudication:
    def test_tie_goes_to_dest(self, env):
        """同一时刻的两份 → 目的地赢。

        这条不是"随便选一个"：`copy2` 保留 mtime，所以"上一轮我们自己搬过去的那
        份"与"这一轮刚复制出来的源"必然打平。平局归目的地，重跑才可能是 no-op。
        """
        write_at(env.dest / "config.json", '{"who": "dest"}', T0)
        write_at(env.documents / "config.json", '{"who": "documents"}', T0)

        # changed=False，但保留了落选副本 → 仍算"动过东西"
        assert env.mod.migrate_user_data() is True

        assert (env.dest / "config.json").read_text(encoding="utf-8") == '{"who": "dest"}'
        loser = env.dest / "legacy_conflict_documents" / "config.json"
        assert loser.read_text(encoding="utf-8") == '{"who": "documents"}'

    def test_legacy_newer_wins_and_dest_copy_is_preserved(self, env):
        """遗留更新 → 采用它，目的地原有那份存进 `legacy_conflict_dest/`。

        目的地那份可能正是"回滚后的旧版又写过一轮"的真数据，绝不能直接扔。
        """
        write_at(env.dest / "config.json", '{"who": "dest"}', T0)
        write_at(env.documents / "config.json", '{"who": "documents"}', T0 + 100)

        assert env.mod.migrate_user_data() is True

        assert (env.dest / "config.json").read_text(encoding="utf-8") == '{"who": "documents"}'
        kept = env.dest / "legacy_conflict_dest" / "config.json"
        assert kept.read_text(encoding="utf-8") == '{"who": "dest"}'
        # 源仍未被删
        assert (env.documents / "config.json").exists()

    def test_two_legacy_roots_newest_wins_and_both_losers_kept(self, env):
        """三方裁决：documents / install / dest 各一份，最新的赢，另两份都留下。

        `install` 就是提权会话写进安装目录的那棵树 —— 数据分裂的另一半。
        """
        write_at(env.dest / "config.json", '{"who": "dest"}', T0)
        write_at(env.documents / "config.json", '{"who": "documents"}', T0 + 100)
        write_at(env.install / "config.json", '{"who": "install"}', T0 + 200)

        assert env.mod.migrate_user_data() is True

        assert (env.dest / "config.json").read_text(encoding="utf-8") == '{"who": "install"}'
        assert (env.dest / "legacy_conflict_dest" / "config.json").read_text(
            encoding="utf-8"
        ) == '{"who": "dest"}'
        assert (env.dest / "legacy_conflict_documents" / "config.json").read_text(
            encoding="utf-8"
        ) == '{"who": "documents"}'
        # 胜者自己不进 conflict 目录
        assert not (env.dest / "legacy_conflict_install").exists()

    def test_state_logical_time_outranks_mtime(self, env):
        """`state/` 比的是 `MAX(updated_at)`，不是文件 mtime。

        目的地那份 mtime 更新（VACUUM、只读打开触发的 checkpoint 都会顶起 mtime），
        但业务上更旧。按 mtime 判会把用户真正最新的任务列表判成"旧的"。
        """
        make_state(env.dest, mtime=T0 + 5000, logical=1000.0)
        make_state(env.documents, mtime=T0, logical=2000.0)

        assert env.mod.migrate_user_data() is True

        db = env.dest / "state" / "tasks" / "tasks.db"
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT MAX(updated_at) FROM tasks").fetchone()[0] == 2000.0
        finally:
            conn.close()
        # 落选的目的地副本被完整保留
        assert (env.dest / "legacy_conflict_dest" / "state" / "tasks" / "tasks.db").exists()

    def test_state_newest_transaction_only_in_wal_still_wins(self, env):
        """胜者的最新事务**只在 `-wal` 里**时也必须被看见。

        WAL 模式下主库 mtime 几乎不动，而目的地那份可能因 checkpoint / VACUUM
        显得"更新"。只看物理时间就会把用户刚下的一批任务判成旧数据丢掉 ——
        逻辑时间读的是副本（先复制再裁决），SQLite 打开时会重放 WAL。
        """
        state, conn = make_wal_state(env.documents, mtime=T0, committed=1000.0, wal_only=5000.0)
        try:
            # 目的地：逻辑时间更旧，但物理时间**远新** —— 只有逻辑时间能判对。
            make_state(env.dest, mtime=T0 + 9000, logical=2000.0)
            assert env.mod._state_wall_time(state) == T0

            assert env.mod.migrate_user_data() is True
        finally:
            conn.close()

        db = env.dest / "state" / "tasks" / "tasks.db"
        merged = sqlite3.connect(str(db))
        try:
            assert merged.execute("SELECT MAX(updated_at) FROM tasks").fetchone()[0] == 5000.0
        finally:
            merged.close()
        assert env.mod._MIGRATION_FAILURES == []

    def test_state_falls_back_to_wall_time_when_logical_missing(self, env):
        """有任何一个候选读不出逻辑时间 → 整批退化为物理时间。

        逻辑时间是业务 epoch，wall time 是文件 mtime，两把尺子混着比毫无意义。
        """
        make_state(env.dest, mtime=T0)  # 无 tasks.db → 拿不到逻辑时间
        make_state(env.documents, mtime=T0 + 5000, logical=2000.0)

        assert env.mod.migrate_user_data() is True
        assert (env.dest / "state" / "tasks" / "tasks.db").exists()


class TestLogMerge:
    def test_same_name_different_content_gets_fixed_tag_suffix(self, env):
        """日志不做胜负判定 —— 同名不同内容一律并存，后缀是**固定的 tag**。

        递增后缀（`_2`、`_3`）会让最容易触发重跑的那批用户磁盘里长出一堆副本。
        """
        write_at(env.dest / "logs" / "app.log", "dest log", T0)
        write_at(env.documents / "logs" / "app.log", "documents log", T0 + 100)
        write_at(env.documents / "logs" / "only_legacy.log", "legacy only", T0)

        assert env.mod.migrate_user_data() is True

        logs = env.dest / "logs"
        assert (logs / "app.log").read_text(encoding="utf-8") == "dest log"
        assert (logs / "app_documents.log").read_text(encoding="utf-8") == "documents log"
        assert (logs / "only_legacy.log").read_text(encoding="utf-8") == "legacy only"

        # 重跑：既不再复制，也绝不出现 app_documents_2.log
        before = walk_set(logs)
        env.mod.take_migration_report()
        assert env.mod.migrate_user_data() is False
        assert walk_set(logs) == before
        assert env.mod._MIGRATION_CONFLICTS == []

    def test_identical_log_is_skipped(self, env):
        """内容一致（就是上次搬过去的那份）→ 完全跳过，不记冲突。"""
        write_at(env.dest / "logs" / "app.log", "same bytes", T0)
        write_at(env.documents / "logs" / "app.log", "same bytes", T0)

        assert env.mod.migrate_user_data() is False
        assert walk_set(env.dest / "logs") == {"app.log"}
        assert env.mod._MIGRATION_CONFLICTS == []


class TestSqliteAwareHelpers:
    def test_state_wall_time_counts_wal_but_not_shm(self, paths_mod, tmp_path):
        """物理时间 = `max(mtime(db), mtime(db-wal))`。

        `-wal` 里就是尚未 checkpoint 的最新事务，漏掉它会把"刚写完一批任务"的库
        判成旧的。`-shm` 只是共享内存索引，跟业务新旧无关，绝不能参与。
        """
        state = tmp_path / "state"
        db = state / "tasks" / "tasks.db"
        write_at(db, "db", T0)
        write_at(db.with_name("tasks.db-wal"), "wal", T0 + 500)
        write_at(db.with_name("tasks.db-shm"), "shm", T0 + 9000)

        assert paths_mod._state_wall_time(state) == T0 + 500

    def test_tree_signature_excludes_sqlite_sidecars(self, paths_mod, tmp_path):
        """指纹必须排除伴生文件 —— 否则"读一下有多新"这个纯读操作会改变指纹。

        最后一个连接关闭时 SQLite 会 passive checkpoint 并删掉 `-wal`/`-shm`，
        探测前后文件数不同，同一份数据会被判成两份不同的数据。
        """
        root = tmp_path / "state"
        write_at(root / "notes.txt", "hello", T0)
        write_at(root / "tasks" / "tasks.db", "db", T0)
        for sidecar in ("tasks.db-wal", "tasks.db-shm", "tasks.db-journal"):
            write_at(root / "tasks" / sidecar, "noise", T0 + 9000)

        count, _total, newest = paths_mod._tree_signature(root)
        assert count == 2
        assert newest == T0


class TestMigrationFailures:
    def test_copy_failure_blocks_the_marker(self, env, monkeypatch):
        """任何一项失败 → 不写 `.migrated_v2`，下次启动重试。

        写早了：被回滚的旧版继续往旧路径写数据，而下次更新看到标记就跳过迁移、
        直接采用陈旧副本 —— 静默的数据丢失。
        """
        write_at(env.documents / "config.json", '{"who": "documents"}', T0)
        write_at(env.documents / "update_manifest_cache.json", "{}", T0)

        real_copy = env.mod._copy_item

        def flaky(src, dst):
            if dst.name == "config.json":
                raise OSError("device busy")
            real_copy(src, dst)

        monkeypatch.setattr(env.mod, "_copy_item", flaky)

        env.mod.migrate_user_data()

        assert env.mod._MIGRATION_OK is False
        assert env.mod.commit_migration_marker() is False
        assert not (env.dest / env.mod.MIGRATION_MARKER).exists()
        # 没失败的那项照常搬过去 —— 迁移是逐项 best-effort，不是全有或全无
        assert (env.dest / "update_manifest_cache.json").exists()

    def test_take_report_does_not_launder_a_failure_into_a_marker(self, env, monkeypatch):
        """`take_migration_report()` 清队列但**绝不清 `_MIGRATION_OK`**。

        `log_startup_info()` 比 `finalize_startup()` 先跑；若成败标志被一起取走，
        commit 就会在有失败的情况下照样落标记，正好绕开"零失败才写"。
        """
        write_at(env.documents / "config.json", '{"who": "documents"}', T0)
        monkeypatch.setattr(
            env.mod, "_copy_item", lambda src, dst: (_ for _ in ()).throw(OSError("nope"))
        )

        env.mod.migrate_user_data()
        _log, failures, _conflicts = env.mod.take_migration_report()

        assert failures  # 队列确实被取走了
        assert env.mod._MIGRATION_FAILURES == []
        assert env.mod._MIGRATION_OK is False
        assert env.mod.commit_migration_marker() is False

    def test_take_report_preserves_success_flag(self, env):
        """成功路径同理：先回放日志，后 commit，标记照样写得出来。"""
        write_at(env.documents / "config.json", '{"who": "documents"}', T0)

        assert env.mod.migrate_user_data() is True
        env.mod.take_migration_report()
        assert env.mod._MIGRATION_OK is True
        assert env.mod.commit_migration_marker() is True
        assert (env.dest / env.mod.MIGRATION_MARKER).exists()

    def test_failed_preservation_aborts_the_overwrite(self, env, monkeypatch):
        """保不住旧的就别动新的。

        `_preserve_loser()` 返回 False 时整项跳过 —— 否则一次失败的保留会直接吃掉
        目的地那份，而它可能正是回滚后旧版写下的真数据。
        """
        write_at(env.dest / "config.json", '{"who": "dest"}', T0)
        write_at(env.documents / "config.json", '{"who": "documents"}', T0 + 100)

        real_install = env.mod._install_item

        def guard(staged, target):
            if "legacy_conflict_dest" in target.parts:
                raise OSError("read-only")
            real_install(staged, target)

        monkeypatch.setattr(env.mod, "_install_item", guard)

        env.mod.migrate_user_data()

        # 目的地原封不动，源也原封不动 —— 下次启动重试
        assert (env.dest / "config.json").read_text(encoding="utf-8") == '{"who": "dest"}'
        assert (env.documents / "config.json").exists()
        assert env.mod._MIGRATION_OK is False
        assert env.mod.commit_migration_marker() is False


class TestMigrationIdempotency:
    """重跑幂等 —— 这一组是"磁盘长蘑菇"的专用闸门。

    迁移**一定会被反复走**：`.migrated_v2` 故意延迟到 READY 才写，所以任何在
    READY 之前崩溃/被回滚的启动都会让下一次启动重跑整套迁移。只要命名里带序号、
    时间戳或 uuid，最容易触发重跑的那批用户磁盘里就会长出一堆 `legacy_conflict_2/`
    与 `app_documents_2.log`。
    """

    @staticmethod
    def _build_conflicting_tree(env) -> None:
        """三方全冲突：config / state / logs 每一类都有三份互不相同的数据。"""
        write_at(env.dest / "config.json", '{"who": "dest"}', T0)
        write_at(env.documents / "config.json", '{"who": "documents"}', T0 + 100)
        write_at(env.install / "config.json", '{"who": "install"}', T0 + 200)

        make_state(env.dest, mtime=T0, logical=1000.0)
        make_state(env.documents, mtime=T0, logical=2000.0)
        make_state(env.install, mtime=T0, logical=3000.0)

        write_at(env.dest / "logs" / "app.log", "dest log", T0)
        write_at(env.documents / "logs" / "app.log", "documents log", T0)
        write_at(env.install / "logs" / "app.log", "install log", T0)

    def test_rerun_changes_nothing_and_reports_no_new_conflicts(self, env):
        self._build_conflicting_tree(env)

        # ── 第一次：真正做事，冲突副本落地 ────────────────────────
        assert env.mod.migrate_user_data() is True
        _log, failures, conflicts = env.mod.take_migration_report()
        assert failures == []
        assert conflicts  # 确实是个有冲突的场景，否则这个测试什么都没验证
        assert (env.dest / "config.json").read_text(encoding="utf-8") == '{"who": "install"}'
        snapshot = walk_set(env.dest)
        assert "legacy_conflict_dest/config.json" in snapshot
        assert "legacy_conflict_documents/config.json" in snapshot
        assert "logs/app_documents.log" in snapshot
        assert "logs/app_install.log" in snapshot

        # ── 第二次 / 第三次：必须是彻底的 no-op ───────────────────
        for _round in range(2):
            assert env.mod.migrate_user_data() is False
            _log, failures, conflicts = env.mod.take_migration_report()
            assert failures == []
            # 同一份数据绝不重复告警 —— 否则每次启动都弹一次 InfoBar
            assert conflicts == []
            assert walk_set(env.dest) == snapshot

        # 序号后缀一个都不许出现
        assert not any("_2" in name for name in walk_set(env.dest))
        assert env.mod._MIGRATION_OK is True

    def test_rerun_after_marker_committed_is_a_full_short_circuit(self, env):
        """走完整条链（迁移 → commit 标记）之后，下次启动连扫描都不做。"""
        self._build_conflicting_tree(env)

        assert env.mod.migrate_user_data() is True
        assert env.mod.commit_migration_marker() is True
        snapshot = walk_set(env.dest)

        assert env.mod.migrate_user_data() is False
        assert walk_set(env.dest) == snapshot
