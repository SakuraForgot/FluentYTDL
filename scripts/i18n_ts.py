"""``.ts`` 翻译目录的最小读写层（i18n_export_untranslated / i18n_import_translated 共用）。

**为什么不用 ``xml.etree.ElementTree`` 往返？**

``ElementTree.write()`` 会把 lupdate 生成的文件改得面目全非，而这些改动全是纯噪音：

* 丢掉第二行的 ``<!DOCTYPE TS>``（历史上 ``i18n_import_translated.py`` 正是这么丢的）；
* XML 声明写成单引号 ``<?xml version='1.0' encoding='utf-8'?>``；
* 空元素折叠成 ``<name />``，而 lupdate 写 ``<name></name>``；
* 文本里的 ``'`` 不再转义成 ``&apos;``。

于是下一次 ``i18n_update.py`` 一跑，lupdate 把文件重新规范化，产出上万行**语义为零**的
diff —— 真正的译文改动被埋在里面，没人看得见。

所以这里改成**按字节区间原地替换**：只重写要改的那一段 ``<translation>…</translation>``，
文件其余部分逐字节保持原样。代价是依赖 .ts 的规整格式，收益是 diff 里只剩真正改了的行。

依赖的格式前提（已对仓库内中英文 .ts 全量核对，并由 tests/test_i18n_integrity.py 守住）：

* 元素集合仅 ``TS/context/name/message/location/source/extracomment/translation``；
* 无 ``numerus="yes"`` / ``<numerusform>``（复数形式会让"一个 translation 一段文本"不成立）；
* ``<message>`` 不带属性，``<context>``/``<message>`` 不嵌套；
* 实体只用命名式（``&amp;`` ``&lt;`` ``&gt;`` ``&quot;`` ``&apos;``），无 ``&#NN;`` 数字引用。
"""

import re
from dataclasses import dataclass
from pathlib import Path

_CONTEXT_RE = re.compile(r"<context>(?P<body>.*?)</context>", re.DOTALL)
_NAME_RE = re.compile(r"<name>(?P<text>.*?)</name>", re.DOTALL)
_MESSAGE_RE = re.compile(r"<message>(?P<body>.*?)</message>", re.DOTALL)
_SOURCE_RE = re.compile(r"<source>(?P<text>.*?)</source>", re.DOTALL)
# 属性只吃 `名="值"` 形式，这样自闭合的 `/>` 不会被 attrs 吞掉。
_TRANSLATION_RE = re.compile(
    r"<translation(?P<attrs>(?:\s+[\w:.-]+=\"[^\"]*\")*)\s*(?:/>|>(?P<text>.*?)</translation>)",
    re.DOTALL,
)
_TYPE_RE = re.compile(r"\btype=\"(?P<kind>[^\"]*)\"")

#: 已死条目：源码里不存在了，既不该被翻译，也不该被"复活"成活跃条目。
DEAD_KINDS = frozenset({"vanished", "obsolete"})

# 顺序有意义：解码时 ``&amp;`` 必须最后处理，否则 ``&amp;lt;`` 会被二次解码成 ``<``。
_DECODE = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&"))
# 编码时 ``&`` 必须最先处理，否则刚插入的 ``&lt;`` 会被再转义成 ``&amp;lt;``。
_ENCODE = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&apos;"))


def unescape(text: str) -> str:
    """XML 文本 → 原文。"""
    for entity, char in _DECODE:
        text = text.replace(entity, char)
    return text


def escape(text: str) -> str:
    """原文 → XML 文本（与 lupdate 的转义集一致）。"""
    for char, entity in _ENCODE:
        text = text.replace(char, entity)
    return text


@dataclass(frozen=True)
class Entry:
    """一条 ``<message>`` 里的译文，附带它在原文里的字节区间。"""

    context: str
    source: str
    #: ``<translation>`` 的 ``type``：``""`` / ``unfinished`` / ``vanished`` / ``obsolete``
    kind: str
    translation: str
    #: ``<translation …>…</translation>`` 整个元素在原文中的 ``(start, end)``
    span: tuple[int, int]

    @property
    def is_dead(self) -> bool:
        return self.kind in DEAD_KINDS

    @property
    def is_empty(self) -> bool:
        """空译文 == Qt 回退到源串（中文），这正是 ISSUE #88 最直接的成因。"""
        return not self.translation.strip()


def parse(raw: str) -> list[Entry]:
    """扫出全部译文条目，按文件出现顺序返回。"""
    entries: list[Entry] = []
    for ctx in _CONTEXT_RE.finditer(raw):
        lo, hi = ctx.span("body")
        name = _NAME_RE.search(raw, lo, hi)
        context = unescape(name.group("text")) if name else ""
        for msg in _MESSAGE_RE.finditer(raw, lo, hi):
            mlo, mhi = msg.span("body")
            source = _SOURCE_RE.search(raw, mlo, mhi)
            translation = _TRANSLATION_RE.search(raw, mlo, mhi)
            if source is None or translation is None:
                continue
            body = translation.group("text")  # 自闭合 `<translation/>` 时为 None
            kind = _TYPE_RE.search(translation.group("attrs"))
            entries.append(
                Entry(
                    context=context,
                    source=unescape(source.group("text")),
                    kind=kind.group("kind") if kind else "",
                    translation=unescape(body) if body else "",
                    span=translation.span(),
                )
            )
    return entries


def render(text: str) -> str:
    """生成替换用的 ``<translation>`` 元素。

    有译文就落成不带 ``type`` 的形式 —— 这才是 .ts 里"已完成"的表示法，
    也是原先 ``del translation.attrib["type"]`` 想做但做错了的事：那句会连
    ``type="vanished"`` 一起剥掉，把已死条目复活成活跃条目。
    """
    return f"<translation>{escape(text)}</translation>"


def apply_updates(raw: str, updates: list[tuple[Entry, str]]) -> str:
    """把 ``(条目, 新译文)`` 列表落进原文，只动 ``<translation>`` 那几段。"""
    pieces: list[str] = []
    cursor = 0
    for entry, text in sorted(updates, key=lambda item: item[0].span[0]):
        start, end = entry.span
        if start < cursor:
            raise ValueError(f"译文区间重叠（{entry.context}/{entry.source[:30]!r}），.ts 解析有误")
        pieces.append(raw[cursor:start])
        pieces.append(render(text))
        cursor = end
    pieces.append(raw[cursor:])
    return "".join(pieces)


def read(path: Path) -> tuple[str, list[Entry]]:
    """读原文与条目。走 bytes 而非文本模式：换行符必须逐字节保持原样。"""
    raw = path.read_bytes().decode("utf-8")
    return raw, parse(raw)


def write(path: Path, raw: str) -> None:
    """写回原文。同样走 bytes，避免 Windows 上把 ``\\n`` 悄悄变成 ``\\r\\n``。"""
    path.write_bytes(raw.encode("utf-8"))
