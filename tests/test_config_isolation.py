"""config_manager 数据目录隔离契约。

设了 ``FLUENTYTDL_DATA_DIR_OVERRIDE``（updater 降权启动 / 测试 conftest）后，数据目录必须
完全自洽：即便仓库根摆着一份 legacy ``config.json``，也一律不回退——缺 config 就吃
``DEFAULT_CONFIG``。这道闸是修「开发机私有 config.json 漏进测试」那类隔离泄漏的根
（`test_parse_cache.py` 的 cookie-mode 断言正因此曾在开发机翻车）。本文件用同一份 legacy
文件、只翻转 ``override`` 一个变量，证明「翻转 override 就决定 legacy 认不认」。

`_load_config` 只读 `self.config_file` + 类级 `DEFAULT_CONFIG`，故绕开单例 `__init__`
（它会短路并触碰真实路径），用 `object.__new__` 造裸实例直接测这个纯函数。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-cfgiso-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.core.config_manager import ConfigManager  # noqa: E402

# fluentytdl.core 的 __init__ 把单例 `config_manager` 实例再导出，名字撞上同名子模块，
# 于是 `import ... as x` / `from ... import x` 都会绑到实例而非模块。要 monkeypatch 模块级
# 的 data_dir_override / legacy_config_path，必须从 sys.modules 拿真正的子模块对象。
cm_mod = sys.modules["fluentytdl.core.config_manager"]


def _load(monkeypatch, config_file: Path) -> dict:
    # `_load_config` 是纯函数：只读 self.config_file + 类级 DEFAULT_CONFIG，返回新 dict，
    # 不改 self.config。借单例实例调它（QObject 不能 object.__new__），临时换掉 config_file，
    # monkeypatch 结束自动还原，测完不留痕。
    inst = cm_mod.config_manager
    monkeypatch.setattr(inst, "config_file", config_file, raising=False)
    return inst._load_config()


def _write_legacy(path: Path, value: bool) -> None:
    # youtube_cookies_enabled 是干净的布尔哨兵：DEFAULT 为 True，任何迁移都不碰它。
    path.write_text(json.dumps({"youtube_cookies_enabled": value}), encoding="utf-8")


def _patch_paths(monkeypatch, tmp_path, *, override, legacy):
    monkeypatch.setattr(cm_mod, "data_dir_override", lambda: override)
    monkeypatch.setattr(cm_mod, "legacy_config_path", lambda: legacy)
    monkeypatch.setattr(cm_mod, "old_user_data_dir", lambda: tmp_path / "old_docs")
    monkeypatch.setattr(cm_mod, "_migrate_file", lambda *a, **k: None)


def test_override_ignores_repo_root_legacy(monkeypatch, tmp_path):
    # override 指向的新目录里没有 config.json，但仓库根 legacy 摆了一份 False。
    newdir = tmp_path / "override"
    newdir.mkdir()
    legacy = tmp_path / "legacy_config.json"
    _write_legacy(legacy, False)  # 与 DEFAULT True 相反的哨兵

    _patch_paths(monkeypatch, tmp_path, override=str(newdir), legacy=legacy)
    loaded = _load(monkeypatch, newdir / "config.json")

    # legacy 的 False 被无视 → 取 DEFAULT_CONFIG 的 True
    assert loaded["youtube_cookies_enabled"] is True
    assert (
        loaded["youtube_cookies_enabled"] == ConfigManager.DEFAULT_CONFIG["youtube_cookies_enabled"]
    )


def test_no_override_still_reads_legacy(monkeypatch, tmp_path):
    # 对照组：同一份 legacy False，唯一变量是 override 关掉 → 必须读到 legacy。
    # 这正是 override 要挡住的那个泄漏，锁死「差异确实来自这道闸」。
    newdir = tmp_path / "newloc"
    newdir.mkdir()
    legacy = tmp_path / "legacy_config.json"
    _write_legacy(legacy, False)

    _patch_paths(monkeypatch, tmp_path, override="", legacy=legacy)
    loaded = _load(monkeypatch, newdir / "config.json")

    assert loaded["youtube_cookies_enabled"] is False


def test_override_missing_config_yields_pure_defaults(monkeypatch, tmp_path):
    # override 且哪里都没 config.json → 整份就是 DEFAULT_CONFIG，一个字段都不多不少。
    newdir = tmp_path / "override"
    newdir.mkdir()
    missing_legacy = tmp_path / "nope.json"  # 故意不创建

    _patch_paths(monkeypatch, tmp_path, override=str(newdir), legacy=missing_legacy)
    loaded = _load(monkeypatch, newdir / "config.json")

    for key, default in ConfigManager.DEFAULT_CONFIG.items():
        assert loaded[key] == default, key
