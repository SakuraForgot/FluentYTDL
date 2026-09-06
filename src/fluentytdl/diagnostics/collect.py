"""诊断行收集：从 yt-dlp 的输出流里挑出值得喂给 ``diagnose()`` 的那些行。

三个调用点（``executor`` 的下载路径、``workers`` 的轻量提取路径、``yt_dlp_cli``
的解析路径）以前各写一套字符串判断，于是"哪些行算线索"在三处慢慢分叉。最典型的
后果是 ``[download] ... Skipping`` 这类**没有级别前缀**的行：``output_parser``
把它判成 ``type="status"``，下载路径的收集分支只认 ``warning/error/info``，于是
``engine._match_skip_line`` / ``_match_bare_line`` 这两条规则通道在下载路径上
**从来没拿到过输入** —— 规则在，输入永远到不了。这个模块就是那三处共用的唯一定义。

**判定标准刻意绑定在引擎的识别能力上**，而不是一张手写关键字表。一行值得留下，
当且仅当：

1. ``parse_events()`` 能对它产出事件 —— 即"带 ``ERROR:`` / ``WARNING:`` 前缀"
   或"命中某条 ``bareLine`` 规则 / 过滤器跳过通道"；
2. 或者它是 yt-dlp 自己的 ``[info]`` 决策频道（``Downloading 1 format(s): 315+251``
   这类，人读起来是关键上下文，进错误对话框的原始输出区）。

因此新增线索的正确做法是往 ``assets/error_rules.json`` 加规则（无级别前缀的记得带
``bareLine: true``），**不是**回到调用方补一句 ``if "xxx" in line``。这里刻意不维护
POT / auth / toolchain 之类的关键字清单，就是为了让规则表始终是唯一的真源。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator

from .engine import parse_events, parse_level, strip_ansi
from .rules import RuleSet

#: 与 ``executor.diag_lines`` 的历史上限一致：足够装下一整轮下载的警告，
#: 又不至于让一条几百行的载荷灌进错误对话框。
DEFAULT_MAXLEN = 400

#: 去重表的硬上限。正常一次任务的**不同**诊断行是两位数，撞到这个数只可能是
#: 超长播放列表在刷同类警告 —— 那时放弃去重（行数仍受 maxlen 约束）比让集合无限涨好。
_SEEN_CAP = 4096

_INFO_PREFIX = "[info]"


def is_diagnostic_line(line: str, rule_set: RuleSet | None = None) -> bool:
    """单行判定：这一行是否值得进诊断缓冲区。

    判据顺序按代价排：绝大多数行是进度行，会一路落到最后那次规则匹配，所以两个
    廉价判据放在前面短路。
    """
    clean = strip_ansi(line).strip()
    if not clean:
        return False
    level, _ = parse_level(clean)
    if level is not None:
        return True
    if clean[: len(_INFO_PREFIX)].lower() == _INFO_PREFIX:
        return True
    # 剩下的只可能靠 bareLine / 过滤器跳过通道命中。直接问引擎，而不是在这里
    # 复制一份规则匹配 —— 复制出来的那份迟早和规则表分叉。
    return bool(parse_events(clean, rule_set))


class DiagnosticLineCollector:
    """流式诊断行缓冲区，是 ``executor`` / ``workers`` 里那个裸 deque 的替代品。

    契约（三条，由 ``tests/test_diagnostic_collect.py`` 逐条锁死）：

    1. **保持原始出现顺序** —— ``pick_primary`` 的第三级仲裁按 ``line_no`` 取靠后者，
       重排会改判主因。
    2. **同一原始行最多留一份** —— 一行可能既是 ``WARNING:`` 又命中 toolchain 关键字，
       分轮扫描会存两份；更现实的是 yt-dlp 对每个格式 / 每种语言各刷一条同样的警告，
       不去重就会把真正的主因挤出这 400 格的窗口。重复对 ``pick_primary`` 毫无价值
       （它取的是 max），对窗口却是纯损耗。
    3. **不改写原文** —— 存进来的是原样的行（只裁掉尾部空白），语义化留给
       ``parse_events()``。ANSI 剥离只用于生成去重键，不落到存储内容上。
    """

    def __init__(self, *, maxlen: int = DEFAULT_MAXLEN, rule_set: RuleSet | None = None) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._seen: set[str] = set()
        self._dedup = True
        self._rule_set = rule_set

    def feed(self, line: str) -> bool:
        """喂一行原始输出，返回它是否被留下。"""
        if not is_diagnostic_line(line, self._rule_set):
            return False
        if self._dedup:
            key = strip_ansi(line).strip()
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > _SEEN_CAP:
                # 主动关掉去重，而不是清空后继续 —— 清空会让早先出现过的行重新涌进来，
                # 那比不去重更糟。关掉之后这张表不再被查，顺手清掉省内存。
                self._dedup = False
                self._seen.clear()
        self._lines.append(line.rstrip())
        return True

    def feed_text(self, text: str) -> int:
        """喂一整段输出（解析路径拿到的是完整 blob），返回新留下的行数。"""
        return sum(1 for raw in (text or "").splitlines() if self.feed(raw))

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def as_text(self) -> str:
        """拼成 ``diagnose()`` / ``parse_events()`` 接受的多行文本。"""
        return "\n".join(self._lines)

    def clear(self) -> None:
        self._lines.clear()
        self._seen.clear()
        self._dedup = True

    def __iter__(self) -> Iterator[str]:
        return iter(self._lines)

    def __len__(self) -> int:
        return len(self._lines)


def extract_diagnostic_lines(
    output: str,
    rule_set: RuleSet | None = None,
    *,
    maxlen: int = DEFAULT_MAXLEN,
) -> list[str]:
    """一次性版本：整段输出 → 诊断行列表。契约同 ``DiagnosticLineCollector``。"""
    collector = DiagnosticLineCollector(maxlen=maxlen, rule_set=rule_set)
    collector.feed_text(output)
    return collector.lines
