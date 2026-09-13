"""Audit application text without importing the application or changing catalogs."""

from __future__ import annotations

import argparse
import ast
import json
import re
import string
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u4e00-\u9fff]")
MARKERS = {
    "tr",
    "translate",
    "QT_TRANSLATE_NOOP",
    "tr_text",
    "log_text",
    "english",
    "standalone_text",
}


def call_name(node) -> str:
    return (
        node.attr
        if isinstance(node, ast.Attribute)
        else node.id
        if isinstance(node, ast.Name)
        else ""
    )


def placeholder_fields(text: str) -> list:
    return sorted(
        (field, spec, conversion)
        for _, field, spec, conversion in string.Formatter().parse(text)
        if field is not None
    )


def inspect_sources(root: Path = ROOT) -> tuple[list[dict], list[dict], list[str]]:
    marked, unmarked, errors = [], [], []
    paths = sorted((root / "src/fluentytdl").rglob("*.py")) + [root / "main.py"]
    for path in paths:
        if path.name == "ui_text.py":
            # Generated copies are not independent call sites.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        relative = path.relative_to(root).as_posix()
        bilingual_nodes = set()
        if relative == "src/fluentytdl/utils/control_center_text.py":
            for assignment in tree.body:
                if not isinstance(assignment, ast.Assign) or not any(
                    isinstance(target, ast.Name) and target.id == "MESSAGES"
                    for target in assignment.targets
                ):
                    continue
                if not isinstance(assignment.value, ast.Dict):
                    errors.append(f"{relative}:{assignment.lineno}: expected a literal catalog")
                    continue
                for key, pair in zip(assignment.value.keys, assignment.value.values, strict=True):
                    try:
                        source, english = ast.literal_eval(pair)
                        assert isinstance(source, str) and source.strip()
                        assert (
                            isinstance(english, str) and english.strip() and not HAN.search(english)
                        )
                        assert placeholder_fields(source) == placeholder_fields(english)
                    except (AssertionError, TypeError, ValueError, SyntaxError):
                        errors.append(f"{relative}:{pair.lineno}: invalid bilingual catalog entry")
                        continue
                    bilingual_nodes.update(ast.walk(pair))
                    marked.append(
                        dict(
                            file=relative,
                            line=pair.lineno,
                            source=source,
                            english=english,
                            context="ControlCenter",
                            interface="bilingual_pair",
                            message_id=ast.literal_eval(key),
                        )
                    )
        for node in ast.walk(tree):
            if node in bilingual_nodes:
                continue
            if isinstance(node, ast.Call) and call_name(node.func) in MARKERS:
                name = call_name(node.func)
                index = (
                    2
                    if name == "log_text"
                    else 1
                    if name in {"translate", "QT_TRANSLATE_NOOP"}
                    else 0
                )
                if len(node.args) > index:
                    arg = node.args[index]
                    if isinstance(arg, (ast.JoinedStr, ast.BinOp)) or (
                        isinstance(arg, ast.Call)
                        and call_name(arg.func) in {"format", "format_map"}
                    ):
                        errors.append(
                            f"{relative}:{node.lineno}: formatted source passed to {name}"
                        )
                    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        context = "RuntimeText"
                        if name in {"translate", "QT_TRANSLATE_NOOP"}:
                            context = getattr(node.args[0], "value", None)
                        elif name == "tr":
                            owner = parents.get(node)
                            while owner is not None and not isinstance(owner, ast.ClassDef):
                                owner = parents.get(owner)
                            context = owner.name if owner is not None else None
                        marked.append(
                            dict(
                                file=relative,
                                line=node.lineno,
                                source=arg.value,
                                interface=name,
                                context=context,
                            )
                        )
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            parent = parents.get(node)
            if isinstance(parent, (ast.Expr, ast.JoinedStr)):
                continue
            if isinstance(node, ast.Constant):
                value = node.value if isinstance(node.value, str) else ""
            else:
                value = "".join(
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            if not HAN.search(value):
                continue
            ancestor = parent
            localized = False
            while ancestor is not None:
                if isinstance(ancestor, ast.Call) and call_name(ancestor.func) in MARKERS:
                    name = call_name(ancestor.func)
                    index = (
                        2
                        if name == "log_text"
                        else 1
                        if name in {"translate", "QT_TRANSLATE_NOOP"}
                        else 0
                    )
                    if len(ancestor.args) > index and node in ast.walk(ancestor.args[index]):
                        localized = True
                        break
                ancestor = parents.get(ancestor)
            if not localized:
                unmarked.append(dict(file=relative, line=node.lineno, source=value))
    return marked, unmarked, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    marked, unmarked, errors = inspect_sources()
    translations = {}
    for context in (
        ET.parse(ROOT / "assets/locales/fluentytdl_en_US.ts").getroot().findall("context")
    ):
        for message in context.findall("message"):
            target = message.find("translation")
            if target is not None and target.get("type") not in {"vanished", "obsolete"}:
                translations[(context.findtext("name"), message.findtext("source"))] = target
    for entry in marked:
        if entry["interface"] == "bilingual_pair":
            entry["status"] = "bilingual-catalog-verified"
            continue
        translation = translations.get((entry["context"], entry["source"]))
        if translation is None or translation.get("type") or not (translation.text or "").strip():
            errors.append(
                f"{entry['file']}:{entry['line']}: missing finished catalog entry ({entry['context']}, {entry['source']!r})"
            )
        else:
            entry["english"] = translation.text
            entry["status"] = "catalog-verified; runtime acceptance by module in docs/I18N.md"
    exceptions = json.loads((ROOT / "docs/i18n-exceptions.json").read_text(encoding="utf-8"))
    allowed = {(entry["file"], entry["source"]): entry["reason"] for entry in exceptions}
    for entry in unmarked:
        reason = allowed.get((entry["file"], entry["source"]))
        if not reason:
            errors.append(f"{entry['file']}:{entry['line']}: unreviewed text {entry['source']!r}")
        entry["reason"] = reason
    if errors:
        raise SystemExit("\n".join(errors))
    if args.write_report:
        result = dict(
            marked_calls=len(marked),
            reviewed_exceptions=len(unmarked),
            entries=marked,
            exceptions=unmarked,
        )
        (ROOT / "docs/i18n-audit.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    print(f"i18n audit passed: {len(marked)} marked calls, {len(unmarked)} reviewed exceptions")


if __name__ == "__main__":
    main()
