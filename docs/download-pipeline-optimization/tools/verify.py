"""Document-only baseline and link checks. Never imports product modules."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

PLAN = Path(__file__).resolve().parents[1]
ROOT = PLAN.parents[1]
EVIDENCE = PLAN / "evidence"
BASE = ROOT / "docs/architecture-v2/evidence/inventory.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name: str, value: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def capture() -> None:
    dest = EVIDENCE / "baseline.json"
    if dest.exists():
        raise SystemExit("Baseline already exists; investigate drift rather than replacing it.")
    old = json.loads(BASE.read_text(encoding="utf-8"))
    records = []
    drift = []
    for group in old["roots"].values():
        for item in group["files"]:
            path = Path(group["path"]) / item["path"]
            digest = sha(path) if path.is_file() else None
            records.append({"path": str(path), "sha256": digest})
            if digest != item["sha256"]:
                drift.append({"path": str(path), "architecture_v2": item["sha256"], "current": digest})
    for path in sorted((ROOT / "docs/architecture-v2").rglob("*.md")):
        records.append({"path": str(path), "sha256": sha(path)})
    for path in sorted((ROOT / "specs/media-metadata").glob("*.md")):
        records.append({"path": str(path), "sha256": sha(path)})
    state = subprocess.run(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True, capture_output=True, check=True).stdout
    write("baseline.json", {"captured_at": datetime.now(timezone.utc).isoformat(), "git_status": state,
        "records": records, "architecture_drift": drift,
        "limits": "Inherited 366-source-file whitelist plus V2 Markdown and existing metadata spec; new unlisted files are not detected. No user data read."})
    print(json.dumps({"recorded_files": len(records), "architecture_drift": drift}, ensure_ascii=False))


def anchors(path: Path) -> set[str]:
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            result.add(re.sub(r"[^\w\- ]", "", match[1].lower()).replace(" ", "-"))
    return result


def verify() -> int:
    failures = []
    refs = {}
    links = graphs = 0
    docs = sorted(PLAN.rglob("*.md"))
    for doc in docs:
        body = doc.read_text(encoding="utf-8-sig")
        if len(re.findall(r"^```", body, re.M)) % 2:
            failures.append(f"Unbalanced fence: {doc.name}")
        graphs += len(re.findall(r"^```mermaid", body, re.M))
        for match in re.finditer(r"\[[^\]\n]*\]\(([^)\n]+)\)", body):
            url = unquote(match[1].strip("<>"))
            if re.match(r"^(?:https?://|mailto:)", url):
                continue
            name, _, fragment = url.partition("#")
            target = (doc.parent / name).resolve() if name else doc
            links += 1
            if target in {(EVIDENCE / "checks.json").resolve(), (EVIDENCE / "source-references.json").resolve()}:
                continue
            if not target.exists():
                failures.append(f"Missing: {doc.name} -> {url}")
                continue
            if fragment.startswith("L") and re.fullmatch(r"L\d+", fragment):
                count = len(target.read_text(encoding="utf-8-sig").splitlines())
                if not 1 <= int(fragment[1:]) <= count:
                    failures.append(f"Line out of range: {doc.name} -> {url}")
            elif fragment and target.suffix == ".md" and fragment not in anchors(target):
                failures.append(f"Unknown heading: {doc.name} -> {url}")
            if target.suffix in {".py", ".ts", ".toml", ".ps1"}:
                refs.setdefault(str(target), []).append({"doc": doc.relative_to(PLAN).as_posix(), "anchor": fragment})
    baseline = json.loads((EVIDENCE / "baseline.json").read_text(encoding="utf-8"))
    for item in baseline["records"]:
        path = Path(item["path"])
        current = sha(path) if path.is_file() else None
        if current != item["sha256"]:
            failures.append(f"Baseline changed: {path}")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "markdown": len(docs), "local_links": links,
        "mermaid": graphs, "baseline_files": len(baseline["records"]), "referenced_source_files": len(refs),
        "failures": failures, "limits": "Basic links, line bounds, heading slugs, fences and listed-file hashes only; no semantic proof, new-file detection, rendering, product tests, builds or performance measurement."}
    write("checks.json", report)
    write("source-references.json", refs)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return bool(failures)


if __name__ == "__main__":
    if sys.argv[1:] == ["capture"]:
        capture()
    elif sys.argv[1:] == ["verify"]:
        raise SystemExit(verify())
    else:
        raise SystemExit("Usage: verify.py capture|verify")
