"""py7zr 的版本钉子必须三处一致。

`py7zr` 是 updater 解压 app-core 归档的**唯一手段**。它被钉在三个互不相通的
地方：

===============================================  ==================================
位置                                              作用
===============================================  ==================================
`pyproject.toml` 的 `build` extra                 本地 `uv sync --extra build`
`.github/workflows/release.yml::PY7ZR_VERSION`    CI 里装进 updater 的构建环境
`scripts/updater.spec` 的硬失败提示                 缺包时告诉人该装哪个版本
===============================================  ==================================

三处漂移的后果不对称：CI 装的版本与 spec 里 `collect_submodules('py7zr')` 收到的
子模块集合一旦不匹配，打出来的 `updater.exe` 会在**用户机器上**解压归档时才炸
—— 那时新版二进制已经替换到一半。所以这条一致性只能在提交前用测试守住。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"
_WORKFLOW = _ROOT / ".github" / "workflows" / "release.yml"
_SPEC = _ROOT / "scripts" / "updater.spec"

#: `py7zr==X.Y.Z`，允许等号两侧无空格（requirement 规范写法）
_REQ_RE = re.compile(r"py7zr==([0-9]+(?:\.[0-9]+)*)")
#: `PY7ZR_VERSION: '1.1.3'` —— 引号可选，单双引号都收
_ENV_RE = re.compile(r"""PY7ZR_VERSION:\s*['"]?([0-9]+(?:\.[0-9]+)*)['"]?""")


def _pins(pattern: re.Pattern[str], path: Path) -> list[str]:
    return pattern.findall(path.read_text(encoding="utf-8"))


def test_pyproject_pins_an_exact_version():
    """`build` extra 里必须是 `==` 精确钉死，不能是 `>=` / `~=`。"""
    pins = _pins(_REQ_RE, _PYPROJECT)
    assert pins, "pyproject.toml 里找不到 py7zr==X.Y.Z"
    assert len(set(pins)) == 1, f"pyproject.toml 内部就已经不一致: {pins}"


def test_workflow_declares_the_version_as_env():
    pins = _pins(_ENV_RE, _WORKFLOW)
    assert pins, "release.yml 里找不到 PY7ZR_VERSION"
    assert len(set(pins)) == 1, f"release.yml 内部就已经不一致: {pins}"


def test_spec_hard_failure_message_quotes_the_same_version():
    """spec 的缺包提示里那条 `pip install` 必须指向同一个版本。

    提示里写错版本比不写更糟：照着装完仍然对不上 CI，而错误信息本身看起来
    "已经解决了"。
    """
    pins = _pins(_REQ_RE, _SPEC)
    assert pins, "scripts/updater.spec 的硬失败提示里找不到 py7zr==X.Y.Z"
    assert len(set(pins)) == 1, f"updater.spec 内部就已经不一致: {pins}"


def test_all_three_pins_agree():
    pyproject = set(_pins(_REQ_RE, _PYPROJECT))
    workflow = set(_pins(_ENV_RE, _WORKFLOW))
    spec = set(_pins(_REQ_RE, _SPEC))

    assert pyproject == workflow == spec, (
        "py7zr 版本钉子出现漂移 —— "
        f"pyproject.toml={sorted(pyproject)}, "
        f"release.yml={sorted(workflow)}, "
        f"updater.spec={sorted(spec)}"
    )


def test_spec_still_hard_fails_on_a_missing_py7zr():
    """spec 里那道断言不能被"顺手删掉"。

    没有它，本地构建能**成功**产出一个不含 py7zr 的 `updater.exe` —— 它在用户
    机器上解压 app-core 时才失败，而那时旧版二进制已经被换掉了。
    """
    text = _SPEC.read_text(encoding="utf-8")
    assert "collect_submodules('py7zr')" in text
    assert "updater.exe 构建中止" in text
