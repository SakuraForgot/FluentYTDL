"""Destructive installer acceptance, exclusively on ephemeral GitHub-hosted runners."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from release_pipeline import ROOT, validate_result


def run(executable: Path, *arguments: str) -> None:
    subprocess.run([str(executable), *arguments], check=True, timeout=600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-isolated-runner", action="store_true")
    args = parser.parse_args()
    if (
        not args.allow_isolated_runner
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise SystemExit("Installer acceptance may run only on an ephemeral GitHub-hosted runner")
    result = validate_result(ROOT / "build/latest-result.json")
    setup = Path(next(a["path"] for a in result["artifacts"] if a["name"].endswith("-setup.exe")))
    data_root = Path(os.environ["LOCALAPPDATA"]) / "FluentYTDL"
    if data_root.exists():
        raise SystemExit("Refusing an existing user data directory in installer acceptance")
    reports = ROOT / "build/installer-tests"
    reports.mkdir(parents=True, exist_ok=True)
    completed = []
    for scope in ("CURRENTUSER", "ALLUSERS"):
        for language in ("english", "chinesesimp"):
            with tempfile.TemporaryDirectory(prefix="FluentYTDL 中文 path (1) ") as tmp:
                install = Path(tmp) / "application"
                log = reports / f"{scope}-{language}.log"
                args = (
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    f"/{scope}",
                    f"/LANG={language}",
                    f"/DIR={install}",
                    f"/LOG={log}",
                    "/TASKS=addtopath",
                )
                run(setup, *args)
                seed = "zh_CN" if language == "chinesesimp" else "en_US"
                assert (install / "install-language.txt").read_text(
                    encoding="utf-8-sig"
                ).strip() == seed
                run(install / "FluentYTDL.exe", "--build-self-test", str(Path(tmp) / "smoke"))
                for root in (install, data_root):
                    for name in (
                        "config.json",
                        "state/tasks/tasks.db",
                        "bin/dle_user/accounts.json",
                        "bin/cookies_youtube.txt",
                    ):
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("{}", encoding="utf-8")
                movie = install / "downloaded-video.mp4"
                unknown = install / "user-note.txt"
                movie.write_bytes(b"downloaded media")
                unknown.write_text("keep", encoding="utf-8")
                run(setup, *args)
                assert (install / "bin/dle_user/accounts.json").read_text() == "{}"
                assert (data_root / "state/tasks/tasks.db").is_file()
                run(
                    install / "unins000.exe",
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    f"/LOG={log}.uninstall",
                )
                for root in (install, data_root):
                    for name in (
                        "config.json",
                        "state/tasks/tasks.db",
                        "bin/dle_user/accounts.json",
                        "bin/cookies_youtube.txt",
                    ):
                        assert not (root / name).exists(), str(root / name)
                assert movie.read_bytes() == b"downloaded media"
                assert unknown.read_text() == "keep"
                completed.append({"scope": scope, "language": language, "status": "passed"})
                (reports / "results.json").write_text(
                    json.dumps(completed, indent=2), encoding="utf-8"
                )


if __name__ == "__main__":
    main()
