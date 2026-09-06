"""i18n 自检：翻译目录与源码的一致性（ISSUE #88 的防回归网）。

#88 的教训是：翻译漂移不会报错，只会让英文界面静默回退成中文源串。这四类断言
分别钉住当时同时存在的四个坑：

1. 空译文 —— ``<translation type="unfinished" />`` 为空时 Qt 回退到源串（中文）；
2. 上下文缺失 —— 运行期按 ``FixRegistry`` 查表，而 .ts 里根本没有这个 context；
3. 诊断文案漏译 —— 错误弹窗是 #88 的主现场，逐码断言有英文；
4. 本地 translate helper —— 把 context 藏进包装函数，lupdate 抽不到，属结构性错误。

第 5 组守的是**工具链**而非文案：``scripts/i18n_ts.py`` 靠按字节区间原地替换来避免
``ElementTree`` 往返带来的全文重排，代价是依赖 .ts 的规整格式。这些断言就是那份
格式契约 —— lupdate 换版本后若产出 numerus 之类的新结构，这里先红，而不是等到
某次导入静默改坏 1600 条译文。

第 6 组守的是**导入语义**：旧版 ``i18n_import_translated.py`` 会跨 context 误译、
把 vanished 条目复活、并丢掉 ``<!DOCTYPE TS>``。这三条各有一个用例钉住，输入是本文件里
手写的合成 .ts —— 不引用真实文件的条目数，免得断言随翻译进度天天变红。

这些都是**数据/结构**层面的断言：跑不起 Qt 也能验证，因此不需要 QApplication。
"""

import ast
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# Resolve src/ and scripts/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import i18n_export_untranslated  # noqa: E402
import i18n_import_translated  # noqa: E402
import i18n_ts  # noqa: E402

from fluentytdl.diagnostics.catalog import _ENTRIES, _FIX_HINTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "fluentytdl"
EN_TS = ROOT / "assets" / "locales" / "fluentytdl_en_US.ts"

#: 只标记、不翻译的上下文：源串在别处用 QT_TRANSLATE_NOOP 标出，运行期才查表。
MARKER_FUNCS = frozenset({"QT_TRANSLATE_NOOP", "translate"})


def _load_ts(path: Path) -> dict[str, dict[str, str]]:
    """.ts → ``{context: {source: translation}}``，跳过 vanished/obsolete。

    vanished 条目是"源码里已经不存在"的历史包袱，既不该断言它有译文，
    也不该让它顶替同名的活跃条目。
    """
    if not path.exists():
        pytest.skip(f"翻译文件不存在: {path}")
    catalog: dict[str, dict[str, str]] = {}
    for ctx in ET.parse(path).getroot().findall("context"):
        name = ctx.findtext("name") or ""
        bucket = catalog.setdefault(name, {})
        for msg in ctx.findall("message"):
            node = msg.find("translation")
            if node is None or node.get("type") in ("vanished", "obsolete"):
                continue
            bucket[msg.findtext("source") or ""] = node.text or ""
    return catalog


EN = _load_ts(EN_TS)


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _call_name(node: ast.Call) -> str:
    """取被调函数的末段名：``QCoreApplication.translate`` -> ``translate``。"""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _literal_context(node: ast.Call) -> str | None:
    """第一个实参是字符串字面量时返回它，否则 None（动态 context 无法静态校验）。"""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


# --------------------------------------------------------------------------
# 1. 空译文
# --------------------------------------------------------------------------


def test_en_us_has_no_empty_translations() -> None:
    """空译文 == Qt 回退到中文源串，这是 #88 最直接的成因。"""
    empty = [
        (ctx, src)
        for ctx, entries in EN.items()
        for src, text in entries.items()
        if not text.strip()
    ]
    assert not empty, f"en_US 存在 {len(empty)} 条空译文（英文界面会显示中文），例如: {empty[:5]}"


# --------------------------------------------------------------------------
# 2. 源码里的显式 context 必须在 .ts 中存在
# --------------------------------------------------------------------------


def _source_contexts() -> dict[str, list[str]]:
    """扫源码里所有显式写死的 translate context → 出现位置。"""
    found: dict[str, list[str]] = {}
    for path in _py_files():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in MARKER_FUNCS:
                continue
            context = _literal_context(node)
            # 两个实参才是 (context, text)；单参的 self.tr("...") 不在此列。
            if context and len(node.args) >= 2:
                found.setdefault(context, []).append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )
    return found


SOURCE_CONTEXTS = _source_contexts()


def test_source_contexts_were_discovered() -> None:
    """扫描器本身的自检：一条都没扫到说明 AST 规则写坏了，后面的断言会假绿。"""
    assert len(SOURCE_CONTEXTS) >= 5, f"只扫到 {len(SOURCE_CONTEXTS)} 个 context，扫描逻辑可疑"


@pytest.mark.parametrize("context", sorted(SOURCE_CONTEXTS), ids=lambda c: c)
def test_source_context_exists_in_catalog(context: str) -> None:
    """运行期按 context 查表，.ts 里没有这个 context 就永远查不到译文。

    #88 里 ``FixRegistry`` / ``DownloadCard`` / ``FormatSelector`` 三个 context
    在 .ts 中完全不存在，32 条文案因此永久落空 —— 本断言就是那道本该拦住它的网。
    """
    assert context in EN, (
        f"context {context!r} 在源码中被使用（{SOURCE_CONTEXTS[context][:3]}），"
        f"但 assets/locales/fluentytdl_en_US.ts 里没有它 —— "
        f"请运行 python scripts/i18n_update.py 重新抽取"
    )


# --------------------------------------------------------------------------
# 3. 诊断文案（错误弹窗，#88 主现场）逐条有英文
# --------------------------------------------------------------------------

DIAG_SOURCES = sorted(
    {text for entry in _ENTRIES.values() for text in entry} | set(_FIX_HINTS.values())
)


@pytest.mark.parametrize("source", DIAG_SOURCES, ids=lambda s: s[:40])
def test_diagnostics_text_has_english(source: str) -> None:
    """错误弹窗的标题/正文/按钮引导词必须有英文，否则英文用户看到整块中文。"""
    entries = EN.get("Diagnostics", {})
    assert source in entries, f"Diagnostics 上下文里没有源串 {source[:50]!r}（未抽取？）"
    assert entries[source].strip(), f"Diagnostics 源串 {source[:50]!r} 没有英文译文"


# --------------------------------------------------------------------------
# 4. 不允许把 context 藏进本地 translate helper
# --------------------------------------------------------------------------


def _translate_wrappers() -> list[tuple[str, str, str]]:
    """找出"把 context 写死在包装函数里、文本靠形参传入"的 helper。

    ``pyside6-lupdate`` 是按**调用点的函数名 + 字面量实参**扫描的。写成

        def tr(text): return QCoreApplication.translate("FixRegistry", text)

    之后，lupdate 只看到 ``tr("提示")``，把源串抽到**空 context**，而运行期却按
    ``FixRegistry`` 查表 —— 两边永远对不上（#88 的 32 条就是这么丢的）。

    返回 ``(相对路径:行号, 函数名, context)``。
    """
    hits: list[tuple[str, str, str]] = []
    for path in _py_files():
        tree = _parse(path)
        if tree is None:
            continue
        # 本模块用 QT_TRANSLATE_NOOP 显式标了哪些 context？标了的属正确范式。
        marked = {
            ctx
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) == "QT_TRANSLATE_NOOP"
            and len(node.args) >= 2
            and (ctx := _literal_context(node))
        }
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in func.args.args} | {a.arg for a in func.args.posonlyargs}
            for node in ast.walk(func):
                if not isinstance(node, ast.Call) or _call_name(node) != "translate":
                    continue
                if len(node.args) < 2:
                    continue
                context = _literal_context(node)
                # 文本实参是形参（而非字面量）⇒ lupdate 在这里抽不到任何源串。
                text_is_param = isinstance(node.args[1], ast.Name) and (node.args[1].id in params)
                if context and text_is_param and context not in marked:
                    hits.append(
                        (
                            f"{path.relative_to(ROOT).as_posix()}:{func.lineno}",
                            func.name,
                            context,
                        )
                    )
    return hits


def test_no_hidden_context_translate_helpers() -> None:
    """把 context 藏进 helper 又不用 QT_TRANSLATE_NOOP 标源串 = 译文永久落空。

    正确写法二选一：调用点直接写
    ``QCoreApplication.translate("Ctx", "字面量")``，或按 ``diagnostics/catalog.py``
    的范式在声明处用 ``QT_TRANSLATE_NOOP("Ctx", "字面量")`` 标记、读取时再翻译。
    """
    hits = _translate_wrappers()
    detail = "\n".join(f"  {loc} def {name}() -> context {ctx!r}" for loc, name, ctx in hits)
    assert not hits, (
        f"发现 {len(hits)} 个隐藏 context 的 translate helper（lupdate 抽不到源串）:\n{detail}"
    )


# --------------------------------------------------------------------------
# 5. .ts 格式契约（scripts/i18n_ts.py 的原地替换依赖它）
# --------------------------------------------------------------------------

LOCALES = ROOT / "assets" / "locales"
TS_FILES = sorted(LOCALES.glob("*.ts"))

#: i18n_ts 的正则只认这些元素；出现新元素说明 lupdate 的输出结构变了。
KNOWN_ELEMENTS = frozenset(
    {"TS", "context", "name", "message", "location", "source", "extracomment", "translation"}
)


def test_ts_files_exist() -> None:
    """扫不到 .ts 的话，下面的 parametrize 会退化成零用例，假绿。"""
    assert len(TS_FILES) >= 4, f"assets/locales 下只找到 {len(TS_FILES)} 个 .ts"


@pytest.mark.parametrize("ts_path", TS_FILES, ids=lambda p: p.name)
def test_ts_format_contract(ts_path: Path) -> None:
    """.ts 必须保持 i18n_ts 假定的规整形态，否则原地替换会改坏文件。"""
    raw = ts_path.read_bytes().decode("utf-8")

    elements = {name.lstrip("<") for name in re.findall(r"<[a-zA-Z][\w]*", raw)}
    assert elements <= KNOWN_ELEMENTS, (
        f"{ts_path.name} 出现未知元素 {sorted(elements - KNOWN_ELEMENTS)} —— "
        f"scripts/i18n_ts.py 的正则不认识它，请先确认原地替换仍安全"
    )
    assert 'numerus="yes"' not in raw and "<numerusform" not in raw, (
        f"{ts_path.name} 出现复数形式：一条 translation 不再只有一段文本，"
        f"scripts/i18n_ts.py 需要先支持 <numerusform> 才能安全改写"
    )
    assert "<message " not in raw, f"{ts_path.name} 的 <message> 带了属性，i18n_ts 的正则只认裸标签"
    assert "&#" not in raw, f"{ts_path.name} 出现数字字符引用，i18n_ts 只处理命名实体"

    # 解析出的条目数必须与 <message> 数一致 —— 少一条就意味着有 message 被静默跳过。
    entries = i18n_ts.parse(raw)
    assert len(entries) == raw.count("<message>"), (
        f"{ts_path.name}: 解析出 {len(entries)} 条，文件里有 {raw.count('<message>')} 个 <message>"
    )


@pytest.mark.parametrize("ts_path", TS_FILES, ids=lambda p: p.name)
def test_ts_rewrite_is_byte_identical(ts_path: Path) -> None:
    """把已完成的译文原样写回，必须逐字节不变。

    这是 ``i18n_import_translated.py`` 全部安全性的地基：只要这条成立，导入就只会改动
    你点名的那几条，不会像旧版 ``ElementTree.write()`` 那样丢 ``<!DOCTYPE TS>``、
    把 ``&apos;`` 还原成 ``'``、把 ``<name></name>`` 折叠成 ``<name />`` ——
    那些改动会让下一次 lupdate 产出上万行纯噪音 diff，真正的译文改动被埋在里面。
    """
    raw = ts_path.read_bytes().decode("utf-8")
    finished = [(e, e.translation) for e in i18n_ts.parse(raw) if e.kind == "" and e.translation]
    assert finished, f"{ts_path.name} 没有已完成条目，无法验证无损改写"

    rewritten = i18n_ts.apply_updates(raw, finished)
    if rewritten != raw:
        at = next(i for i, (a, b) in enumerate(zip(raw, rewritten, strict=False)) if a != b)
        pytest.fail(
            f"{ts_path.name} 原样改写后不一致，首个差异在偏移 {at}:\n"
            f"  原文: {raw[at - 60 : at + 60]!r}\n"
            f"  改写: {rewritten[at - 60 : at + 60]!r}"
        )


@pytest.mark.parametrize("ts_path", TS_FILES, ids=lambda p: p.name)
def test_ts_escape_roundtrip(ts_path: Path) -> None:
    """i18n_ts 的转义集必须与 lupdate 一致，否则译文会被双重转义或漏转义。"""
    raw = ts_path.read_bytes().decode("utf-8")
    bad = [
        e.source
        for e in i18n_ts.parse(raw)
        if i18n_ts.unescape(i18n_ts.escape(e.source)) != e.source
    ]
    assert not bad, f"{ts_path.name} 有 {len(bad)} 条源串转义往返失真，例如 {bad[:3]}"


def test_dead_entries_are_recognized() -> None:
    """vanished/obsolete 必须被识别成"已死"，导入时才会跳过它们。

    旧版 ``del translation.attrib["type"]`` 会连 ``type="vanished"`` 一起剥掉，
    把源码里早已不存在的条目复活成活跃条目。
    """
    raw = EN_TS.read_bytes().decode("utf-8")
    entries = i18n_ts.parse(raw)
    dead = [e for e in entries if e.is_dead]
    assert dead, "en_US 里一条 vanished/obsolete 都没有？解析 type 属性的逻辑可疑"
    assert all(e.kind in i18n_ts.DEAD_KINDS for e in dead)
    assert len(dead) == raw.count('type="vanished"') + raw.count('type="obsolete"')


# --------------------------------------------------------------------------
# 6. 导入语义（i18n_import_translated 的三个历史缺陷）
# --------------------------------------------------------------------------

#: 手写的最小 .ts，刻意复刻仓库里真实存在的两个坑：
#: ``重新登录`` 同源串跨 context 且**译文不同**（en_US 里实际有 4 个 context），
#: 外加一条 ``type="vanished"`` 的已死条目。
#: 用合成输入而非真实 .ts，是为了不把"当前有多少条漏译"这类会天天变的数字写进断言。
SYNTHETIC_TS = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1" language="en_US">
<context>
    <name>Alpha</name>
    <message>
        <location filename="../a.py" line="1"/>
        <source>重新登录</source>
        <translation>Re-login</translation>
    </message>
    <message>
        <location filename="../a.py" line="2"/>
        <source>取消</source>
        <translation type="unfinished"></translation>
    </message>
</context>
<context>
    <name>Beta</name>
    <message>
        <location filename="../b.py" line="1"/>
        <source>重新登录</source>
        <translation>Sign in again</translation>
    </message>
    <message>
        <source>老条目</source>
        <translation type="vanished">Old</translation>
    </message>
</context>
</TS>
"""


def _fixture(tmp_path: Path, payload: dict) -> tuple[Path, Path]:
    """写出合成 .ts 与译文 JSON。走 bytes：换行必须逐字节可控。"""
    ts_path = tmp_path / "t.ts"
    ts_path.write_bytes(SYNTHETIC_TS.encode("utf-8"))
    json_path = tmp_path / "t.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return ts_path, json_path


def _catalog(ts_path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """``(context, 源串) → (type, 译文)``，用于逐条比对改了什么。"""
    raw = ts_path.read_bytes().decode("utf-8")
    return {(e.context, e.source): (e.kind, e.translation) for e in i18n_ts.parse(raw)}


def test_nested_import_only_touches_the_named_context(tmp_path: Path) -> None:
    """分层格式必须按 ``(context, 源串)`` 精确落点。

    旧版按源串**全局**匹配后无条件赋值，同一句中文在不同 context 下会被强行统一 ——
    实测 ``重新登录`` 在 en_US 有 4 个 context 且译文各不相同，跑一次旧版就全踩平。
    """
    ts_path, json_path = _fixture(tmp_path, {"Alpha": {"重新登录": "Log in again"}})
    before = _catalog(ts_path)

    i18n_import_translated.import_translated(ts_path, json_path)
    after = _catalog(ts_path)

    assert after[("Alpha", "重新登录")] == ("", "Log in again"), "分层格式没能命中指定 context"
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {("Alpha", "重新登录")}, (
        f"除目标条目外还改了 {changed - {('Alpha', '重新登录')}}"
    )


def test_flat_import_fills_gaps_without_overwriting(tmp_path: Path) -> None:
    """扁平格式无从分辨 context，因此只填空缺、绝不覆盖已有译文。"""
    ts_path, json_path = _fixture(tmp_path, {"重新登录": "CLOBBERED", "取消": "Cancel"})
    before = _catalog(ts_path)

    i18n_import_translated.import_translated(ts_path, json_path)
    after = _catalog(ts_path)

    assert after[("Alpha", "取消")] == ("", "Cancel"), "空译文应被填上并转为已完成"
    for context in ("Alpha", "Beta"):
        key = (context, "重新登录")
        assert after[key] == before[key], f"{context} 的已有译文被扁平导入覆盖了"


def test_import_never_resurrects_dead_entries(tmp_path: Path) -> None:
    """vanished/obsolete 条目一律不动，且"一条都没定位到"要非零退出。

    旧版 ``del translation.attrib["type"]`` 会把 ``type="vanished"`` 一起剥掉，
    让源码里早已不存在的条目变回活跃条目。
    """
    ts_path, json_path = _fixture(tmp_path, {"Beta": {"老条目": "Resurrected"}})
    before = ts_path.read_bytes()

    with pytest.raises(SystemExit):
        i18n_import_translated.import_translated(ts_path, json_path)

    assert ts_path.read_bytes() == before, "已死条目被改写了（旧版会把它复活成活跃条目）"


def test_import_preserves_doctype_and_is_idempotent(tmp_path: Path) -> None:
    """改一条译文不得动其余任何字节；再导一次必须零改动。

    旧版用 ``ElementTree.write()`` 回写，会丢掉 ``<!DOCTYPE TS>``、把 ``&apos;`` 还原成
    ``'``、把 ``<name></name>`` 折叠成 ``<name />`` —— 下一次 lupdate 因此产出上万行
    纯噪音 diff，真正的译文改动被埋在里面。
    """
    ts_path, json_path = _fixture(tmp_path, {"Alpha": {"重新登录": "Log in again"}})
    i18n_import_translated.import_translated(ts_path, json_path)

    lines = ts_path.read_bytes().decode("utf-8").splitlines()
    assert lines[0] == '<?xml version="1.0" encoding="utf-8"?>', f"XML 声明被改写: {lines[0]!r}"
    assert lines[1] == "<!DOCTYPE TS>", f"DOCTYPE 丢了（ElementTree.write 的老毛病）: {lines[1]!r}"
    assert "<name />" not in "\n".join(lines), "空元素被折叠成 ElementTree 风格"

    original = SYNTHETIC_TS.splitlines()
    assert len(lines) == len(original), f"行数从 {len(original)} 变成 {len(lines)}，有内容被增删"
    differing = [a for a, b in zip(original, lines, strict=True) if a != b]
    assert len(differing) == 1, f"只改了 1 条译文，却有 {len(differing)} 行不同: {differing}"

    snapshot = ts_path.read_bytes()
    i18n_import_translated.import_translated(ts_path, json_path)
    assert ts_path.read_bytes() == snapshot, "重复导入产生了改动，不幂等"


def test_import_rejects_mixed_shape_json(tmp_path: Path) -> None:
    """形状一半分层一半扁平 ⇒ 无法判定语义，必须报错而不是猜。"""
    ts_path, json_path = _fixture(tmp_path, {"Alpha": {"取消": "Cancel"}, "重新登录": "Re-login"})
    with pytest.raises(SystemExit, match="形状不一致"):
        i18n_import_translated.import_translated(ts_path, json_path)


def test_export_groups_by_context(tmp_path: Path) -> None:
    """导出必须按 context 分层，否则同一句中文在多个 context 下被迫共用一条译文。"""
    ts_path = tmp_path / "t.ts"
    ts_path.write_bytes(SYNTHETIC_TS.encode("utf-8"))
    out = tmp_path / "out.json"

    i18n_export_untranslated.export_untranslated(ts_path, out)
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data == {"Alpha": {"取消": ""}}, f"只有 Alpha/取消 是漏译，实际导出 {data}"
    assert "老条目" not in json.dumps(data, ensure_ascii=False), "已死条目不该被导出等人翻译"
