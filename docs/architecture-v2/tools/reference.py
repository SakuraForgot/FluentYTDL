"""Read-only source inventory and documentation checks; never imports application code."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

PLAN = Path(__file__).resolve().parents[1]
ROOT = PLAN.parents[1]
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
            elif resolved.suffix == ".md" and fragment:
                headings = re.findall(r"^#{1,6}\s+(.+)$", resolved.read_text(encoding="utf-8-sig"), re.M)
                anchors = {re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-") for h in headings}
                if fragment not in anchors:
                    failures.append(f"Unresolved heading anchor: {path.relative_to(PLAN)} -> {target}")
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
              "failures": failures, "limits": "Recorded-file hash comparison only; new unlisted files are not detected. Simple Markdown heading slugs only. No source-symbol semantic, Mermaid renderer, product test, build, or runtime validation."}
    (EVIDENCE / "documentation-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(failures))


def indexes() -> None:
    """Generate import candidates and source-to-document navigation, not a call graph."""
    inventory = json.loads((EVIDENCE / "inventory.json").read_text(encoding="utf-8"))
    modules = {}
    for item in inventory["roots"]["client"]["files"]:
        name = item["path"]
        if name.startswith("src/fluentytdl/") and name.endswith(".py"):
            parts = name[4:-3].split("/")
            if parts[-1] == "__init__":
                parts.pop()
            modules[".".join(parts)] = item
    edges = []
    for module, item in modules.items():
        package = module if item["path"].endswith("/__init__.py") else module.rpartition(".")[0]
        for entry in item.get("import_candidates", []):
            node = ast.parse(entry["statement"]).body[0]
            candidates = []
            if isinstance(node, ast.Import):
                candidates = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".")
                    keep = len(parts) - node.level + 1
                    base = ".".join(parts[:max(keep, 0)])
                    if node.module:
                        base = base + "." + node.module
                else:
                    base = node.module or ""
                candidates = [base] + [base + "." + a.name for a in node.names]
            for target in sorted(set(candidates)):
                if target in modules and target != module:
                    edges.append({"from": module, "to": target, "file": item["path"],
                                  "line": entry["line"], "statement": entry["statement"]})
    graph = {m: set() for m in modules}
    for edge in edges:
        graph[edge["from"]].add(edge["to"])
    numbers, low, stack, active, groups = {}, {}, [], set(), []

    def visit(v: str) -> None:
        numbers[v] = low[v] = len(numbers)
        stack.append(v)
        active.add(v)
        for w in sorted(graph[v]):
            if w not in numbers:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in active:
                low[v] = min(low[v], numbers[w])
        if low[v] == numbers[v]:
            group = []
            while True:
                w = stack.pop()
                active.remove(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for v in sorted(graph):
        if v not in numbers:
            visit(v)
    dependency = {"scope": "Python imports within src/fluentytdl, including conditional, type-only and local imports.",
                  "limits": "SCCs are static candidates, not proof of initialization failure. No dynamic calls or Qt signal edges.",
                  "modules": len(modules), "edges": edges, "strongly_connected_candidates": groups}
    (EVIDENCE / "dependency-candidates.json").write_text(json.dumps(dependency, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reverse = {}
    for doc in sorted(PLAN.rglob("*.md")):
        for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", doc.read_text(encoding="utf-8-sig")):
            url = unquote(match.group(1).strip("<>"))
            if re.match(r"^(https?://|mailto:|#)", url):
                continue
            name, _, fragment = url.partition("#")
            source = (doc.parent / name).resolve()
            if source.suffix not in EXTENSIONS or source.is_relative_to(PLAN):
                continue
            key = Path(os.path.relpath(source, ROOT)).as_posix()
            reverse.setdefault(key, []).append({"document": doc.relative_to(PLAN).as_posix(), "anchor": fragment})
    (EVIDENCE / "code-to-docs.json").write_text(json.dumps({"limits": "File-level citations, not inferred call edges or complete symbol coverage.", "references": reverse}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    patterns = {
        "process": r"\b(?:Popen|subprocess\.(?:run|Popen)|multiprocessing\.Process|taskkill)\b",
        "thread_task": r"\b(?:QThread|ThreadPoolExecutor|threading\.Thread|Thread\(|\.submit\()",
        "event_timer": r"(?:\.connect\(|QTimer|\.singleShot\(|\.wait\()",
        "synchronization": r"\b(?:Lock\(|RLock\(|Semaphore\(|Event\(|Queue\()",
        "resource_write_cleanup": r"(?:os\.replace|\.unlink\(|rmtree\(|\.close\(|\.terminate\(|\.kill\(|write_text\(|open\(.+['\"](?:w|a))",
        "retry_recovery": r"\b(?:while |retry|backoff|recover|rollback|watchdog)",
    }
    candidates = []
    for item in modules.values():
        for number, line in enumerate((ROOT / item["path"]).read_text(encoding="utf-8-sig").splitlines(), 1):
            for category, pattern in patterns.items():
                if re.search(pattern, line, re.I):
                    candidates.append({"category": category, "file": item["path"], "line": number, "text": line.strip()[:240]})
    (EVIDENCE / "runtime-candidates.json").write_text(json.dumps({"limits": "Lexical candidates in captured Python source, including comments and inactive code; not proof of runtime wiring or exhaustive semantics.", "candidates": candidates}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"modules": len(modules), "import_edges": len(edges), "scc_candidates": len(groups), "referenced_source_files": len(reverse), "runtime_candidates": len(candidates)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "verify", "index"])
    args = parser.parse_args()
    if args.action == "capture":
        capture()
    elif args.action == "index":
        indexes()
    else:
        raise SystemExit(verify())
