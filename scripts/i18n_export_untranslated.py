"""导出 ``.ts`` 里尚无译文的条目，供人工/AI 逐条补齐。

输出**按 context 分层**：

    {
        "Diagnostics": {"重新登录": ""},
        "MainWindow":  {"重新登录": ""}
    }

历史版本输出扁平的 ``{源串: ""}``，那个形状会把 context 丢掉 —— 而同一句中文在不同
context 下的英文往往不同（``重新登录`` 在 ``Diagnostics`` 是 "Sign in again"，
在 ``MainWindow`` 是 "Re-login"），扁平字典逼着两处共用一条译文。分层之后，
一句中文出现在几个 context 里就有几个待填格子，各填各的。

同时不再导出 ``type="vanished"`` / ``"obsolete"`` 的条目：那是源码里已经不存在的历史包袱，
翻译它们纯属浪费，配上旧版 import 的 ``del attrib["type"]`` 还会把它们复活。

用法::

    python scripts/i18n_export_untranslated.py assets/locales/fluentytdl_en_US.ts out.json
    python scripts/i18n_export_untranslated.py <input.ts> <output.json> --drafts
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_ts import Entry, read  # noqa: E402


def _wanted(entry: Entry, drafts: bool) -> bool:
    """该条目是否需要人工补译？"""
    if entry.is_dead:
        return False
    if entry.is_empty:
        return True
    # 有文本但仍标 unfinished == Qt Linguist 里的"草稿/待复核"。它在界面上**已经生效**，
    # 所以默认不算漏译；--drafts 才捞出来复核。
    return drafts and entry.kind == "unfinished"


def export_untranslated(ts_path: Path, output_path: Path, drafts: bool = False) -> int:
    _, entries = read(ts_path)

    catalog: dict[str, dict[str, str]] = defaultdict(dict)
    for entry in entries:
        if _wanted(entry, drafts):
            # 草稿模式下把现有译文带出去，方便直接改；纯漏译则留空等填。
            catalog[entry.context][entry.source] = entry.translation

    ordered = {ctx: dict(sorted(catalog[ctx].items())) for ctx in sorted(catalog)}
    total = sum(len(items) for items in ordered.values())

    output_path.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )

    print(f"导出 {total} 条待译文案（{len(ordered)} 个 context）→ {output_path}")

    # 一句中文落在多个 context 里 —— 这正是扁平格式会译错的地方，显式点出来。
    seen: dict[str, list[str]] = defaultdict(list)
    for ctx, items in ordered.items():
        for source in items:
            seen[source].append(ctx)
    shared = {src: ctxs for src, ctxs in seen.items() if len(ctxs) > 1}
    if shared:
        print(f"  注意：{len(shared)} 条源串横跨多个 context，请按各自语境分别翻译：")
        for source, contexts in sorted(shared.items())[:10]:
            print(f"    {source[:40]!r} → {', '.join(contexts)}")
        if len(shared) > 10:
            print(f"    …另有 {len(shared) - 10} 条")

    if total == 0:
        hint = "" if drafts else "；加 --drafts 可捞出仍标 unfinished 的草稿条目复核"
        print(f"  {ts_path.name} 没有漏译条目{hint}")

    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 .ts 中待翻译的条目（按 context 分层）")
    parser.add_argument(
        "ts_file", type=Path, help="输入 .ts，例如 assets/locales/fluentytdl_en_US.ts"
    )
    parser.add_argument("output_json", type=Path, help="输出 JSON")
    parser.add_argument(
        "--drafts",
        action="store_true",
        help='连同 type="unfinished" 但已有文本的草稿一起导出（值为现有译文）',
    )
    args = parser.parse_args()

    if not args.ts_file.exists():
        raise SystemExit(f"找不到 .ts 文件: {args.ts_file}")

    export_untranslated(args.ts_file, args.output_json, drafts=args.drafts)


if __name__ == "__main__":
    main()
