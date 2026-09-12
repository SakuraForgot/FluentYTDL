"""架构断言：`download/` 与 `processing/` 里不许自己扫目录、不许自己动文件。

守的是《下载产物事务层》的硬约束 1 与 4：

- **一次物理扫描** —— 全流程只有 `staging.reconcile()` 扫盘，且只扫 `payload/`。
  别处再扫一次就等于给同一批文件重新猜一遍角色，那正是「少删误删」的根因。
- **销毁与位移只经过 `StagingArea` 的原子方法** —— 每一对「动文件 + 改清单」必须
  是一步。写成 `os.rename(...)` 紧跟 `manifest.rename(...)` 的两步式，两步之间抛
  异常就让物理世界与清单分叉。

**纯创建不在名单里**（`shutil.copy2` / `tempfile.mkstemp` / `open(..., "w")`）：被集中
的权威是「销毁与位移」。只往「别人交给它的路径」里写新文件的模块破坏不了事务 ——
产物要么被 `register_generated()` / `replace_artifact_content()` 收养，要么是垃圾，
随沙盒 `rmtree` 消失。

## 为什么用 AST 而不是 grep

1. grep 认不出 `from pathlib import Path` 之后的 `p.iterdir()` —— 而「把 `os.remove(p)`
   优雅重构成 `Path(p).unlink()`」恰恰是最容易发生的那种重构；
2. grep 数不了参数个数，于是 `.replace()` 这个和 `str.replace` 撞名的方法没法判别；
3. grep 会被 docstring 骗到。`processing/subtitle_processor.py` 的模块 docstring 就是
   现成的假阳性样本：它记述 `parent_dir.glob(f"{stem}.*")` 这个**已经退休的历史坑**
   （`stem` 未转义，被 glob 当通配符），文本里带着 `glob` 却一行扫描代码都没有。

本文件只 `ast.parse` 源码，**不 import fluentytdl** —— 所以不需要
`FLUENTYTDL_DATA_DIR_OVERRIDE`，也不会碰真实数据目录。

## 基线：现在是空的

`_KNOWN_VIOLATIONS` 一路从 24 条减到 0 —— Step 3/4/5 把 `download/` 与 `processing/`
里的目录扫描与破坏性调用全部收敛进了 `StagingArea`。基线是**双向断言**：

- 出现基线之外的违规 → 失败（防新增）；
- 基线里的条目已经修好 → 也失败（防基线腐烂）。

所以每个 Step 修完自己那几处必须回来删行，而空基线正是这套断言真正想到达的状态：
从此任何一次命中都是**新增**违规，不必再由人去判断「这条是历史遗留还是刚写坏的」。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "fluentytdl"

#: 扫描范围。`core/` 不在内 —— 硬约束 8（Controller 不得对活事务 rmtree）由
#: `test_download_staging.py` 的端到端用例守，不是靠禁用 API 守。
SCAN_ROOTS: tuple[str, ...] = ("download", "processing")

#: 事务层自己是唯一豁免：它就是那个「唯一一处」。
EXEMPT: frozenset[str] = frozenset({"download/staging.py"})

# ── 名单 ────────────────────────────────────────────────────

#: 目录扫描面（模块函数形式）。
_SCAN_MODULE_APIS: frozenset[str] = frozenset(
    {
        "os.walk",
        "os.listdir",
        "os.scandir",
        "glob.glob",
        "glob.iglob",
    }
)

#: 破坏性 / 位移面（模块函数形式）。`os.removedirs` / `os.renames` 是 `rmdir` /
#: `rename` 的递归版，比被点名的那两个更狠，一并禁掉。
_DESTRUCTIVE_MODULE_APIS: frozenset[str] = frozenset(
    {
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.renames",
        "os.replace",
        "os.rmdir",
        "os.removedirs",
        "shutil.rmtree",
        "shutil.move",
    }
)

#: 目录扫描面（`pathlib` 方法形式）。无条件报警 —— str / list / dict 上都没有这三个。
_SCAN_METHODS: frozenset[str] = frozenset({"glob", "rglob", "iterdir"})

#: 破坏性面里可以无条件报警的方法：这三个名字在标准容器类型上都不存在。
_DESTRUCTIVE_METHODS: frozenset[str] = frozenset({"unlink", "rmdir"})

#: 需要按**实参个数**判别的方法 —— 两个都和别的类型撞名：
#:
#: - `Path.replace(target)` 一个参数；`str.replace(old, new[, count])` 至少两个。
#:   单参数的 `.replace()` 在 str 上是 `TypeError`，所以这条规则没有合法假阳性。
#: - `Path.rename(target)` 一个参数；而 `Manifest.rename(id, new_path)`（事务层自己的
#:   清单 API，Step 5 起会被 Feature 调用）是两个。不按参数个数判就会把清单更新
#:   误报成文件操作。
_ARITY_ONE_METHODS: frozenset[str] = frozenset({"replace", "rename"})

_MODULE_APIS: frozenset[str] = _SCAN_MODULE_APIS | _DESTRUCTIVE_MODULE_APIS

# ── 已知违规基线（修完请删行）─────────────────────────────
#
# 条目形状是 `(模块相对路径, 所在作用域, API)`。**刻意不记行号** —— 上方任何编辑都会
# 让行号漂移，而作用域名字在真正的重构之前是稳定的。
#
# 下面这份消化记录留着，是因为「为什么这里不许扫目录 / 不许 `os.remove`」这个问题的
# 答案就在这些历史坑里，而代码本身已经不再包含它们：
#
# - **Step 3** —— `processing/subtitle_processor.py` 的四级定位（含 `_find_subtitle_files`
#   里那处 `.iterdir()`）整体退休。最后一级会把**整个目录**的字幕交出来，在共享下载
#   目录里那就是删掉别人视频的字幕。
# - **Step 4** —— `workers.py` 的上岸块（`os.walk` + 逐文件 `shutil.move` + 嵌套
#   `get_unique_path()` + `shutil.rmtree`）整体删除，落地收敛进 `staging.commit()`；
#   `_sweep_part_files` / `_clean_part_files` 收缩成 `_finalize_staging_cancel()` /
#   `_finalize_staging_failure()`，「对 `output_path ∪ dest_paths` 无条件 `os.remove`」
#   那段杀伤半径（缺陷 F）随之消失。七行基线一次清掉。
# - **Step 4（一处裁决）** —— `_monitor_section_part_progress` 的 `.glob("*.part")` 改成调
#   `StagingArea.parts_bytes()`。裁决理由不是「它只读字节数所以豁免」，而是那处扫描
#   **本身就是坏的**：它扫 `paths["home"]`，而 `home != temp` 之后 `.part` 全在
#   `.parts/` 里，兜底进度会一路报 0。
# - **Step 5** —— Feature 链改为只动清单：`find_final_merged_file` / `_locate_files` 的
#   `os.listdir`、`_cleanup_external_subtitles` / `_cleanup_thumbnail_files` 的 `os.remove`
#   全部消失（前者换成 `manifest.primary_media()`，后者换成 `drop()` / `mark_degraded()`）；
#   `VRFeature` 的 `os.remove` / `os.replace` 换成 `supersede_artifact()` /
#   `replace_artifact_content()`。
# - **Step 5（⚠ 两处规划漏掉的，由本测试首次发现）** ——
#   `VRFeature._inject_meta` 与 `on_post_process` 同类，规划只点了后者；
#   `AudioProcessor.embed_cover_art` / `normalize_audio_file` 规划完全没提，而它们曾是
#   这一类缺陷里**最危险**的一处：`unlink()` 紧跟 `rename()` 不是原子替换，两行之间
#   断电 = 用户的音频文件直接消失，新文件还挂在临时名上。两者现在都是纯 transformer
#   （只读 input、只写 output、失败不清理 output），采纳交给 `replace_artifact_content()`。

_KNOWN_VIOLATIONS: frozenset[tuple[str, str, str]] = frozenset()


class _Finding(NamedTuple):
    module: str
    scope: str
    api: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.module, self.scope, self.api)

    def __str__(self) -> str:  # pragma: no cover - 只在失败信息里出现
        return f"{self.module}:{self.line} [{self.scope}] {self.api}"


# ── 扫描器 ──────────────────────────────────────────────────


def _collect_aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """`import` 语句 → 两张别名表。**整棵树都走**，函数内的 import 也算。

    Returns:
        `(module_aliases, name_aliases)`：前者是 `import os as o` → `{"o": "os"}`，
        后者是 `from os import remove as rm` → `{"rm": "os.remove"}`。
    """
    module_aliases: dict[str, str] = {}
    name_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module_aliases[alias.asname] = alias.name
                else:
                    # `import os.path` 绑定的是 `os`
                    head = alias.name.split(".")[0]
                    module_aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            # 相对 import 前面留个点，保证拼不出 `os.remove` 这样的名字
            prefix = "." * node.level + (node.module or "")
            for alias in node.names:
                name_aliases[alias.asname or alias.name] = f"{prefix}.{alias.name}"
    return module_aliases, name_aliases


class _ApiScanner(ast.NodeVisitor):
    """找出「对禁用 API 的调用」以及「对它们的裸引用」。

    裸引用也要抓：`functools.partial(shutil.rmtree, d)` 和 `map(os.remove, paths)`
    一样是在动文件，只是把调用推迟了一格。
    """

    def __init__(self, module: str, tree: ast.AST) -> None:
        self.module = module
        self.findings: list[_Finding] = []
        self._scope: list[str] = []
        self._module_aliases, self._name_aliases = _collect_aliases(tree)
        #: 已经作为 Call.func 处理过的节点，避免同一处报两遍
        self._consumed: set[int] = set()

    # -- 作用域 --

    def _push(self, node: ast.AST) -> None:
        self._scope.append(getattr(node, "name", "?"))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._push(node)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._push(node)
        self.generic_visit(node)
        self._scope.pop()

    @property
    def _scope_name(self) -> str:
        return ".".join(self._scope) or "<module>"

    def _report(self, api: str, node: ast.AST) -> None:
        self.findings.append(
            _Finding(self.module, self._scope_name, api, getattr(node, "lineno", 0))
        )

    # -- 调用 --

    def visit_Call(self, node: ast.Call) -> None:
        self._consumed.add(id(node.func))
        func = node.func
        if isinstance(func, ast.Attribute):
            dotted = self._as_module_api(func)
            if dotted is not None:
                # 确定是模块调用了，别再按方法名兜一遍（`glob.glob` 只报一次）
                if dotted in _MODULE_APIS:
                    self._report(dotted, node)
            else:
                self._check_method(func.attr, node)
        elif isinstance(func, ast.Name):
            dotted = self._name_aliases.get(func.id)
            if dotted in _MODULE_APIS:
                self._report(str(dotted), node)
        self.generic_visit(node)

    def _as_module_api(self, func: ast.Attribute) -> str | None:
        """`os.remove` → `"os.remove"`；不是「模块别名.属性」这个形状则 None。"""
        if not isinstance(func.value, ast.Name):
            return None
        target = self._module_aliases.get(func.value.id)
        if target is None:
            return None
        return f"{target}.{func.attr}"

    def _check_method(self, attr: str, node: ast.Call) -> None:
        if attr in _SCAN_METHODS or attr in _DESTRUCTIVE_METHODS:
            self._report(f".{attr}()", node)
            return
        if attr in _ARITY_ONE_METHODS and len(node.args) + len(node.keywords) == 1:
            # `Path.rename(target)` / `Path.replace(target)`，也覆盖 `target=` 关键字写法
            self._report(f".{attr}()", node)

    # -- 裸引用 --

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if id(node) not in self._consumed:
            dotted = self._as_module_api(node)
            if dotted in _MODULE_APIS:
                self._report(str(dotted), node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if id(node) not in self._consumed and isinstance(node.ctx, ast.Load):
            dotted = self._name_aliases.get(node.id)
            if dotted in _MODULE_APIS:
                self._report(str(dotted), node)
        self.generic_visit(node)


def scan_source(source: str, module: str = "<test>") -> list[_Finding]:
    tree = ast.parse(source)
    scanner = _ApiScanner(module, tree)
    scanner.visit(tree)
    return scanner.findings


def _modules() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for root in SCAN_ROOTS:
        base = SRC / root
        assert base.is_dir(), f"扫描根不存在：{base}"
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(SRC).as_posix()
            if rel in EXEMPT:
                continue
            out.append((rel, path))
    return out


def _all_findings() -> list[_Finding]:
    found: list[_Finding] = []
    for rel, path in _modules():
        found.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return found


def _fmt(findings: list[_Finding]) -> str:
    lines = [f"  {f}" for f in sorted(findings, key=lambda f: (f.module, f.line))]
    return "\n".join(lines)


def _baseline_repr(findings: list[_Finding]) -> str:
    """把违规打印成可直接贴进 `_KNOWN_VIOLATIONS` 的形状。"""
    keys = sorted({f.key for f in findings})
    return "\n".join(f'        ("{m}", "{s}", "{a}"),' for m, s, a in keys)


# ── 用例 ────────────────────────────────────────────────────


def test_scan_roots_and_exemption_exist():
    """豁免名单必须指向真实文件 —— 否则一次改名就把豁免悄悄扩大了。"""
    for rel in EXEMPT:
        assert (SRC / rel).is_file(), f"豁免条目不存在：{rel}（改名了？）"


def test_exemption_is_load_bearing():
    """`staging.py` 自己必须命中这些 API。

    它豁免不是因为它干净，而是因为它**就是**那个唯一的执行点。哪天它不再命中，
    说明破坏性动作搬到别处去了 —— 那时豁免本身就该重新审。
    """
    staging = SRC / "download" / "staging.py"
    findings = scan_source(staging.read_text(encoding="utf-8"), "download/staging.py")
    apis = {f.api for f in findings}
    assert apis & (_DESTRUCTIVE_MODULE_APIS | {".unlink()", ".rename()", ".replace()"}), (
        "事务层里一个破坏性/位移调用都没有，那 EXEMPT 还豁免什么？"
    )
    assert apis & (_SCAN_MODULE_APIS | {".glob()", ".rglob()", ".iterdir()"}), (
        "事务层里没有目录扫描，那 `reconcile()` 是怎么对账的？"
    )


def test_no_unlisted_directory_scans():
    """硬约束 4：`reconcile()` 是全流程唯一的目录扫描。"""
    unlisted = [
        f
        for f in _all_findings()
        if (f.api in _SCAN_MODULE_APIS or f.api in {".glob()", ".rglob()", ".iterdir()"})
        and f.key not in _KNOWN_VIOLATIONS
    ]
    assert not unlisted, (
        "新增了目录扫描 —— 角色判定只能发生在 `reconcile()`，别处不许再猜一遍：\n"
        + _fmt(unlisted)
        + "\n\n确实必要的话，贴进 _KNOWN_VIOLATIONS 并写清哪个 Step 会消掉它：\n"
        + _baseline_repr(unlisted)
    )


def test_no_unlisted_destructive_file_ops():
    """硬约束 1：销毁与位移只经过 `StagingArea` 的原子方法。"""
    method_apis = {".unlink()", ".rmdir()", ".rename()", ".replace()"}
    unlisted = [
        f
        for f in _all_findings()
        if (f.api in _DESTRUCTIVE_MODULE_APIS or f.api in method_apis)
        and f.key not in _KNOWN_VIOLATIONS
    ]
    assert not unlisted, (
        "新增了直接的文件销毁/位移 —— 「动文件 + 改清单」必须是 `StagingArea` 上的"
        "一步，两步式会在中间抛异常时让物理世界与清单分叉：\n"
        + _fmt(unlisted)
        + "\n\n确实必要的话，贴进 _KNOWN_VIOLATIONS 并写清哪个 Step 会消掉它：\n"
        + _baseline_repr(unlisted)
    )


def test_baseline_has_no_stale_entries():
    """基线里已经修好的条目必须删掉 —— 否则基线会变成永久许可证。"""
    live = {f.key for f in _all_findings()}
    stale = sorted(_KNOWN_VIOLATIONS - live)
    assert not stale, "这些基线条目已经修好了，请从 _KNOWN_VIOLATIONS 里删掉：\n" + "\n".join(
        f"  {m} [{s}] {a}" for m, s, a in stale
    )


# ── 扫描器自测 ──────────────────────────────────────────────
#
# 一个悄悄失效的架构测试比没有更糟：它会一直绿。


def test_detector_ignores_strings_and_comments():
    """docstring / 注释 / 普通字符串里的同名文本不算命中。

    `processing/subtitle_processor.py` 的模块 docstring 就是现成的样本 —— 它记述
    `parent_dir.glob(...)` 这个**已经退休的**历史坑，文本里带 `glob` 而代码里没有。
    """
    src = '''
"""历史坑：这里以前是 parent_dir.glob(f"{stem}.*") + os.remove(p)。"""
import os

def f(p):
    # os.remove(p) / shutil.rmtree(p) —— 注释里的不算
    note = "os.walk / Path(p).unlink() / .iterdir()"
    return note + os.path.basename(p)
'''
    assert scan_source(src) == []


def test_detector_ignores_pure_creation():
    """纯创建不在任何名单里。"""
    src = """
import shutil, tempfile, os
from pathlib import Path

def f(src_path, dst_path):
    shutil.copy2(src_path, dst_path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst_path))
    Path(dst_path).write_bytes(b"x")
    with open(dst_path, "w", encoding="utf-8") as fh:
        fh.write("y")
    return fd, tmp
"""
    assert scan_source(src) == []


def test_detector_catches_module_level_apis():
    src = """
import os
import shutil
import glob

def f(d):
    for _root, _dirs, _files in os.walk(d):
        pass
    os.listdir(d)
    os.scandir(d)
    glob.glob(d + "/*")
    glob.iglob(d + "/*")
    os.remove(d)
    os.unlink(d)
    os.rename(d, d + "2")
    os.replace(d, d + "2")
    os.rmdir(d)
    os.removedirs(d)
    os.renames(d, d + "2")
    shutil.rmtree(d)
    shutil.move(d, d + "2")
"""
    apis = sorted({f.api for f in scan_source(src)})
    assert apis == sorted(_MODULE_APIS), apis


def test_detector_follows_aliases():
    """`import os as o` / `from shutil import rmtree as nuke` 都不该漏。

    这是 grep 最容易被绕过的地方。
    """
    src = """
import os as o
import shutil as sh
from os import remove
from shutil import rmtree as nuke

def f(p):
    o.replace(p, p + "2")
    sh.move(p, p + "2")
    remove(p)
    nuke(p)
"""
    apis = sorted({f.api for f in scan_source(src)})
    assert apis == ["os.remove", "os.replace", "shutil.move", "shutil.rmtree"]


def test_detector_catches_pathlib_methods():
    """「把 `os.remove(p)` 优雅重构成 `Path(p).unlink()`」不许绕过架构测试。"""
    src = """
from pathlib import Path

def f(p, target):
    base = Path(p)
    base.unlink()
    base.unlink(missing_ok=True)
    base.rmdir()
    base.rename(target)
    base.replace(target)
    base.rename(target=target)
    for child in base.iterdir():
        child.unlink()
    list(base.glob("*.vtt"))
    list(base.rglob("*.srt"))
"""
    apis = sorted({f.api for f in scan_source(src)})
    assert apis == [
        ".glob()",
        ".iterdir()",
        ".rename()",
        ".replace()",
        ".rglob()",
        ".rmdir()",
        ".unlink()",
    ], apis


def test_detector_arity_rule_spares_str_and_manifest_apis():
    """`str.replace(old, new)` 与 `Manifest.rename(id, path)` 是两个参数，不报警。"""
    src = """
def f(name, manifest, art_id, new_path):
    cleaned = name.replace(":", "_").replace("?", "", 1)
    manifest.rename(art_id, new_path)
    return cleaned
"""
    assert scan_source(src) == []


def test_detector_catches_deferred_references():
    """把调用推迟一格（`map` / `partial`）照样算动文件。"""
    src = """
import functools
import os
import shutil

def f(paths, d):
    list(map(os.remove, paths))
    return functools.partial(shutil.rmtree, d)
"""
    apis = sorted({f.api for f in scan_source(src)})
    assert apis == ["os.remove", "shutil.rmtree"]


def test_detector_reports_scope_and_line():
    src = """
import os

class Embedder:
    def embed(self, p):
        os.replace(p, p + "2")
"""
    (finding,) = scan_source(src, "processing/x.py")
    assert finding.module == "processing/x.py"
    assert finding.scope == "Embedder.embed"
    assert finding.api == "os.replace"
    assert finding.line == 6


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
