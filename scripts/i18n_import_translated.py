"""把 JSON 里的译文写回 ``.ts``。

接受两种 JSON 形状：

* **分层（推荐，i18n_export_untranslated.py 的输出）** —— ``{context: {源串: 译文}}``，
  按 ``(context, 源串)`` 精确定位，改哪条就是哪条。
* **扁平（兼容旧文件）** —— ``{源串: 译文}``，只能按源串全局匹配，因此**只填空缺、
  绝不覆盖已有译文**，并对横跨多个 context 的源串报警。

旧版有三个会静默改坏文件的行为，这里逐条修掉：

1. **跨 context 误译** —— 旧版按源串全局匹配后无条件赋值，同一句中文在不同 context 下会被
   强行统一。实测 ``重新登录`` 在本仓库有 4 个 context 且译文各不相同（``Diagnostics``
   是 "Sign in again"，``MainWindow``/``SelectionDialog``/``CookieRepairDialog`` 是
   "Re-login"），跑一次旧版就把它们踩平。
2. **复活已死条目** —— 旧版 ``del translation.attrib["type"]`` 连 ``type="vanished"``
   一起剥掉，把源码里早已不存在的条目变回活跃条目。这里直接跳过 vanished/obsolete。
3. **丢 DOCTYPE / 全文重排** —— 旧版用 ``ElementTree.write()`` 回写，丢掉 ``<!DOCTYPE TS>``
   并改掉转义与空元素写法，下一次 lupdate 会产出上万行纯噪音 diff。改为按字节区间原地替换，
   详见 ``scripts/i18n_ts.py`` 的模块说明。

用法::

    python scripts/i18n_import_translated.py assets/locales/fluentytdl_en_US.ts translated.json
    python scripts/i18n_import_translated.py <input.ts> <translated.json> --dry-run
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_ts import Entry, apply_updates, read, write  # noqa: E402

_UNKNOWN = "JSON 里有、.ts 里查不到（拼错？已 vanished？）"


@dataclass
class _Plan:
    """一次导入要做什么，以及为什么有些条目没做。"""

    updates: list[tuple[Entry, str]] = field(default_factory=list)
    #: JSON 里成功定位到活跃条目的键数。为 0 而 JSON 又非空 ⇒ 文件/形状选错了。
    matched: int = 0
    #: 标签 → 明细，原样打印给用户
    notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, label: str, item: str) -> None:
        self.notes.setdefault(label, []).append(item)


def _load(path: Path) -> tuple[dict, bool]:
    """读 JSON 并判形状，返回 ``(数据, 是否分层)``。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} 顶层必须是对象，实际是 {type(data).__name__}")
    if not data:
        return data, True

    nested = [k for k, v in data.items() if isinstance(v, dict)]
    flat = [k for k, v in data.items() if isinstance(v, str)]
    if len(nested) == len(data):
        return data, True
    if len(flat) == len(data):
        return data, False
    raise SystemExit(
        f"{path} 的形状不一致：{len(nested)} 个键的值是对象、{len(flat)} 个是字符串。"
        f"请统一成 {{context: {{源串: 译文}}}} 或 {{源串: 译文}}"
    )


def _plan_nested(data: dict[str, dict[str, str]], entries: list[Entry]) -> _Plan:
    """分层模式：按 ``(context, 源串)`` 精确定位，改哪条就是哪条。"""
    index = {(e.context, e.source): e for e in entries if not e.is_dead}
    plan = _Plan()

    for context, items in data.items():
        for source, text in items.items():
            if not text:
                continue  # 空值 == 这条还没译，跳过（不要用空串去清掉已有译文）
            entry = index.get((context, source))
            if entry is None:
                plan.note(_UNKNOWN, f"{context}/{source[:40]!r}")
                continue
            plan.matched += 1
            if entry.translation == text:
                continue  # 已经是这个值，不产生 diff
            if not entry.is_empty:
                plan.note("覆盖了已有译文", f"{context}/{source[:40]!r}")
            plan.updates.append((entry, text))

    return plan


def _plan_flat(data: dict[str, str], entries: list[Entry]) -> _Plan:
    """扁平模式：只按源串匹配，因此**只填空缺**，绝不覆盖已有译文。"""
    by_source: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        if not entry.is_dead:
            by_source[entry.source].append(entry)

    plan = _Plan()
    for source, text in data.items():
        if not text:
            continue
        candidates = by_source.get(source)
        if not candidates:
            plan.note(_UNKNOWN, repr(source[:40]))
            continue
        plan.matched += 1
        targets = [e for e in candidates if e.is_empty]
        if not targets:
            plan.note("已有译文，扁平模式不覆盖（要改请用分层格式）", repr(source[:40]))
            continue
        contexts = sorted({e.context for e in targets})
        if len(contexts) > 1:
            plan.note(
                "源串横跨多个 context，这些位置被赋了同一条译文",
                f"{source[:40]!r} → {', '.join(contexts)}",
            )
        plan.updates.extend((entry, text) for entry in targets)

    return plan


def _report(label: str, items: list[str], limit: int = 10) -> None:
    print(f"  {label}（{len(items)} 条）：")
    for item in items[:limit]:
        print(f"    {item}")
    if len(items) > limit:
        print(f"    …另有 {len(items) - limit} 条")


def import_translated(ts_path: Path, json_path: Path, dry_run: bool = False) -> int:
    data, nested = _load(json_path)
    raw, entries = read(ts_path)

    if nested:
        plan = _plan_nested(data, entries)
        supplied = sum(1 for items in data.values() for text in items.values() if text)
    else:
        plan = _plan_flat(data, entries)
        supplied = sum(1 for text in data.values() if text)
        print("扁平 JSON（无 context）：只填空缺、不覆盖已有译文。精确改写请改用分层格式。")

    verb = "将写入" if dry_run else "已写入"
    print(f"{verb} {len(plan.updates)} 条译文 → {ts_path}")
    for label, items in plan.notes.items():
        _report(label, items)

    if plan.updates and not dry_run:
        write(ts_path, apply_updates(raw, plan.updates))

    # 只在**一条都没定位到**时报错：那是选错文件或 context 名对不上。
    # 定位到了但无改动是正常的（重复导入、译文已一致），不该当失败。
    if supplied and not plan.matched:
        raise SystemExit(
            f"JSON 提供了 {supplied} 条译文，却一条都没能在 {ts_path.name} 里定位到 —— "
            f"通常是 context 名对不上或 .ts 选错了文件，请核对后重跑"
        )

    return len(plan.updates)


def main() -> None:
    parser = argparse.ArgumentParser(description="把 JSON 译文写回 .ts（分层或扁平均可）")
    parser.add_argument("ts_file", type=Path, help="目标 .ts")
    parser.add_argument("translated_json", type=Path, help="译文 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只报告要改什么，不写文件")
    args = parser.parse_args()

    for path in (args.ts_file, args.translated_json):
        if not path.exists():
            raise SystemExit(f"找不到文件: {path}")

    import_translated(args.ts_file, args.translated_json, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
