"""发布物内容校验的两道闸门：白名单分类 与 运行期垃圾黑名单。

这两件事故意是两个函数、两套名单，测试也分开写：

* `classify_app_core_items()` —— app-core 归档**只收白名单里的东西**。它真正的
  风险不是"收多了"，而是"以后新增的合法发布物被静默丢掉"，所以 unknown 非空
  必须是构建期硬失败。
* `assert_dist_clean()` —— 三个发布目标（full.7z / app-core / setup.exe）全都调。
  任何人在打包前从 `dist/` 直接启动过程序，那台机器的 `config.json`、`logs/`、
  `state/tasks/tasks.db` 就留在了 dist 里；而 `bin/cookies_*.txt` 与
  `bin/dle_user/` 里是**真实凭据** —— 进了公开归档就是会话泄漏。

按路径加载 `scripts/build.py`（它自己会把 `scripts/` 插进 sys.path，所以
`fetch_tools` / `version_manager` 都能正常解析）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BUILD_FILE = _ROOT / "scripts" / "build.py"
_PYPROJECT = _ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def build_mod():
    spec = importlib.util.spec_from_file_location("_build_under_test", _BUILD_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _single_line_array(key: str) -> list[str]:
    """从 pyproject.toml 里按**单行数组**取值。

    刻意复刻 `build.py::_load_config()` 在没有 tomllib 时的行解析器：数组一旦
    被拆成多行，这里就会拿到空列表 —— 与构建期"静默失效"的表现完全一致，
    因此这个解析器本身就是那条排版约束的测试工具。
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(key)}\s*=\s*\[(.*)\]\s*$", text, re.MULTILINE)
    assert m, f"{key} 未以单行数组形式出现在 pyproject.toml 中"
    return re.findall(r'"([^"]+)"', m.group(1))


class TestBuildConfigArrays:
    """三个数组是构建期硬断言的数据源，绝不能静默变空。"""

    @pytest.mark.parametrize("key", ["app_core_include", "app_core_exclude", "dist_forbidden"])
    def test_array_is_single_line_and_non_empty(self, key):
        assert _single_line_array(key)

    def test_forbidden_covers_user_data_and_real_credentials(self):
        forbidden = set(_single_line_array("dist_forbidden"))
        # 用户数据：进了归档会用开发者的配置覆盖用户的
        assert {"config.json", "logs", "state"} <= forbidden
        # 真实凭据：进了公开归档就是 YouTube 会话泄漏
        assert {"bin/cookies_youtube.txt", "bin/cookies_twitter.txt", "bin/dle_user"} <= forbidden
        # 便携标记只属于 full.7z，写进 dist 会让安装版把数据写进 Program Files
        assert "portable.txt" in forbidden
        # 已废弃的写探测残留 —— 见 utils/paths.py 的双轨解析
        assert ".writetest" in forbidden

    def test_include_and_exclude_do_not_overlap(self):
        inc = set(_single_line_array("app_core_include"))
        exc = set(_single_line_array("app_core_exclude"))
        assert inc & exc == set()
        # updater.exe 运行时被锁，只能以 .new 的形式投递
        assert "updater.exe" in exc
        assert "updater.exe.new" in inc


class TestClassifyAppCoreItems:
    def test_real_config_classifies_a_realistic_dist(self, build_mod):
        inc = _single_line_array("app_core_include")
        exc = _single_line_array("app_core_exclude")
        names = [
            "FluentYTDL.exe",
            "_internal",
            "licenses",
            "LICENSE",
            "BUILD_INFO.json",
            "updater.exe.new",
            "bin",
            "updater.exe",
        ]

        keep, drop, unknown = build_mod.classify_app_core_items(names, inc, exc)

        assert unknown == []
        assert "FluentYTDL.exe" in keep
        assert "updater.exe.new" in keep
        # bin/ 由 full.7z 分发，updater.exe 改走 .new 投递
        assert set(drop) == {"bin", "updater.exe"}

    def test_unregistered_item_lands_in_unknown(self, build_mod):
        """既不在白名单也不在排除名单 → unknown。

        白名单最大的风险是"以后新增的合法发布物被静默丢掉"，这一格就是那盏红灯。
        """
        inc = _single_line_array("app_core_include")
        exc = _single_line_array("app_core_exclude")

        _keep, _drop, unknown = build_mod.classify_app_core_items(
            [*inc, "brand_new_artifact.dat"], inc, exc
        )
        assert unknown == ["brand_new_artifact.dat"]

    def test_runtime_garbage_is_unknown_not_silently_dropped(self, build_mod):
        """`config.json` 不在两张名单里 —— 它必须炸构建，而不是被悄悄丢掉。"""
        inc = _single_line_array("app_core_include")
        exc = _single_line_array("app_core_exclude")

        _keep, drop, unknown = build_mod.classify_app_core_items(
            ["FluentYTDL.exe", "config.json", "state"], inc, exc
        )
        assert unknown == ["config.json", "state"]
        assert drop == []

    def test_output_is_sorted_and_lossless(self, build_mod):
        """三类之和恒等于输入，且顺序稳定（构建日志要可 diff）。"""
        names = ["zeta", "FluentYTDL.exe", "bin", "alpha"]
        keep, drop, unknown = build_mod.classify_app_core_items(names, ["FluentYTDL.exe"], ["bin"])
        assert keep == ["FluentYTDL.exe"]
        assert drop == ["bin"]
        assert unknown == ["alpha", "zeta"]
        assert sorted(keep + drop + unknown) == sorted(names)


class TestAssertDistClean:
    @staticmethod
    def _forbidden() -> list[str]:
        return _single_line_array("dist_forbidden")

    def test_clean_dist_passes(self, build_mod, tmp_path):
        dist = tmp_path / "dist"
        (dist / "_internal").mkdir(parents=True)
        (dist / "FluentYTDL.exe").write_bytes(b"MZ")
        (dist / "bin").mkdir()
        (dist / "bin" / "yt-dlp.exe").write_bytes(b"MZ")

        build_mod.assert_dist_clean(dist, self._forbidden())

    def test_missing_dist_is_not_an_error(self, build_mod, tmp_path):
        """dist 还不存在（首次构建）不是污染。"""
        build_mod.assert_dist_clean(tmp_path / "nope", self._forbidden())

    @pytest.mark.parametrize(
        "relpath",
        [
            "config.json",
            "update_manifest_cache.json",
            "error_rules.override.json",
            "portable.txt",
            ".writetest",
        ],
    )
    def test_runtime_file_aborts_the_build(self, build_mod, tmp_path, relpath):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / relpath).write_text("leftover", encoding="utf-8")

        with pytest.raises(RuntimeError, match="发布目录被运行期产物污染"):
            build_mod.assert_dist_clean(dist, self._forbidden())

    @pytest.mark.parametrize("relpath", ["logs", "state", ".fluent_temp"])
    def test_runtime_dir_aborts_the_build(self, build_mod, tmp_path, relpath):
        dist = tmp_path / "dist"
        (dist / relpath).mkdir(parents=True)

        with pytest.raises(RuntimeError, match="发布目录被运行期产物污染"):
            build_mod.assert_dist_clean(dist, self._forbidden())

    @pytest.mark.parametrize(
        "relpath",
        ["bin/cookies_youtube.txt", "bin/cookies_twitter.txt", "bin/dle_user"],
    )
    def test_real_credentials_abort_the_build(self, build_mod, tmp_path, relpath):
        """这三项是**真实凭据**，不是观感问题 —— 进了公开归档就是会话泄漏。"""
        dist = tmp_path / "dist"
        target = dist / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if relpath.endswith(".txt"):
            target.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        else:
            target.mkdir()

        with pytest.raises(RuntimeError, match="发布目录被运行期产物污染"):
            build_mod.assert_dist_clean(dist, self._forbidden())

    def test_error_message_names_every_hit(self, build_mod, tmp_path):
        """报错必须逐条列出命中项 —— 只说"被污染了"没法修。"""
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "config.json").write_text("{}", encoding="utf-8")
        (dist / "logs").mkdir()

        with pytest.raises(RuntimeError) as exc:
            build_mod.assert_dist_clean(dist, self._forbidden())

        msg = str(exc.value)
        assert "config.json" in msg
        assert "logs" in msg

    def test_bare_name_does_not_match_recursively(self, build_mod, tmp_path):
        """裸名字**不做递归匹配**，这是刻意的。

        `_internal/` 下有几千个第三方包的资源文件，随便一个叫 config.json 的包
        数据都会把整条发布链误判成"被污染"。需要递归时在配置里显式写 `**/name`。
        """
        dist = tmp_path / "dist"
        pkg = dist / "_internal" / "some_package"
        pkg.mkdir(parents=True)
        (pkg / "config.json").write_text('{"third": "party"}', encoding="utf-8")

        build_mod.assert_dist_clean(dist, self._forbidden())

    def test_glob_entry_is_honoured(self, build_mod, tmp_path):
        """含 `*` 的条目按 glob 处理。"""
        dist = tmp_path / "dist"
        (dist / "bin").mkdir(parents=True)
        (dist / "bin" / "cookies_anything.txt").write_text("secret", encoding="utf-8")

        with pytest.raises(RuntimeError, match="发布目录被运行期产物污染"):
            build_mod.assert_dist_clean(dist, ["bin/cookies_*.txt"])
