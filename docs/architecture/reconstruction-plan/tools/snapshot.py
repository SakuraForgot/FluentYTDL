"""Read-only source inventory and documentation checks; never imports application code."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

PLAN = Path(__file__).resolve().parents[1]
ROOT = PLAN.parents[2]
CC = ROOT.parent / "FluentYTDL-ControlCenter"
EVIDENCE = PLAN / "evidence"
EXTENSIONS = {".py", ".ts", ".vue", ".sql", ".json", ".toml", ".yml", ".yaml",
              ".spec", ".iss", ".ps1", ".cmd", ".mjs", ".lock"}
SKIP = {"__pycache__", "node_modules", ".git", ".venv", "dist", "build", ".wrangler"}


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


def files(root: Path, directories: list[str], names: list[str]) -> list[Path]:
    found = {root / name for name in names if (root / name).is_file()}
    for directory in directories:
        parent = root / directory
        if not parent.is_dir():
            continue
        for path in parent.rglob("*"):
            if path.is_file() and path.suffix in EXTENSIONS and not SKIP.intersection(path.relative_to(parent).parts):
                found.add(path)
    return sorted(found)


def describe(path: Path, root: Path) -> dict:
    raw = path.read_bytes()
    item = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "lines": len(raw.splitlines())}
    if path.suffix == ".py":
        try:
            tree = ast.parse(raw.decode("utf-8-sig"), filename=str(path))
            symbols = []

            def definitions(node: ast.AST, prefix: str = "") -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        name = prefix + child.name
                        symbols.append({"name": name, "kind": type(child).__name__, "line": child.lineno})
                        definitions(child, name + ".")
                    else:
                        definitions(child, prefix)

            definitions(tree)
            item["symbols"] = symbols
            item["import_candidates"] = [
                {"line": n.lineno, "statement": ast.unparse(n)} for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))
            ]
        except (SyntaxError, UnicodeError) as exc:
            item["parse_error"] = str(exc)
    return item


def capture() -> None:
    client_names = [p.name for p in ROOT.glob("*.py")]
    client_names += ["pyproject.toml", "uv.lock", "build-environment.json", "VERSION"]
    client = files(ROOT, ["src", "scripts", "tests", ".github/workflows", "installer", "packaging"], client_names)
    center = files(CC, ["apps", "packages", "migrations", "scripts", "tests"],
                   ["package.json", "package-lock.json", "tsconfig.json", "wrangler.toml"])
    output = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Selected source, tests, build and deployment configuration; no user data or generated binaries.",
        "limitations": "AST candidates only; not a call graph, dynamic import proof, or runtime acceptance.",
        "client_head": git("rev-parse", "HEAD"), "client_branch": git("branch", "--show-current"),
        "roots": {
            "client": {"path": str(ROOT), "files": [describe(p, ROOT) for p in client]},
            "control_center": {"path": str(CC), "files": [describe(p, CC) for p in center]},
        },
    }
    EVIDENCE.mkdir(exist_ok=True, parents=True)
    (EVIDENCE / "inventory.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"client_files": len(client), "control_center_files": len(center),
                      "python_parse_errors": sum("parse_error" in f for r in output["roots"].values() for f in r["files"])}, ensure_ascii=False))


def verify() -> int:
    failures = []
    docs = sorted(PLAN.rglob("*.md"))
    count = 0
    mermaid = 0
    for path in docs:
        body = path.read_text(encoding="utf-8-sig")
        mermaid += body.count("```mermaid")
        if body.count("```") % 2:
            failures.append(f"Unbalanced code fences: {path.relative_to(PLAN)}")
        for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", body):
            target = unquote(match.group(1).strip("<>"))
            if re.match(r"^(https?://|mailto:|#)", target):
                continue
            name, _, fragment = target.partition("#")
            name = re.sub(r":\d+$", "", name)
            resolved = (path.parent / name).resolve()
            count += 1
            if resolved == (EVIDENCE / "documentation-check.json").resolve():
                # This output is created below, including on the first verification run.
                continue
            if not resolved.exists():
                failures.append(f"Missing link: {path.relative_to(PLAN)} -> {target}")
            elif resolved.is_file() and re.fullmatch(r"L\d+", fragment):
                line = int(fragment[1:])
                if not 1 <= line <= len(resolved.read_bytes().splitlines()):
                    failures.append(f"Invalid source line: {path.relative_to(PLAN)} -> {target}")
    inventory = json.loads((EVIDENCE / "inventory.json").read_text(encoding="utf-8"))
    hashes = 0
    for group in inventory["roots"].values():
        for item in group["files"]:
            path = Path(group["path"]) / item["path"]
            hashes += 1
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                failures.append(f"Source drift: {path}")
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "markdown_files": len(docs),
              "local_links": count, "mermaid_blocks": mermaid, "source_hashes": hashes,
              "failures": failures, "limits": "Recorded-file hash comparison only; new unlisted files are not detected. No semantic link-anchor, Mermaid renderer, product test, build, or runtime validation."}
    (EVIDENCE / "documentation-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "verify"])
    args = parser.parse_args()
    if args.action == "capture":
        capture()
    else:
        raise SystemExit(verify())
