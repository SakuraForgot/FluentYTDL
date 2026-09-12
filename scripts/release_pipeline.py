"""Shared release policy and immutable artifact publication (no rebuilds)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from build import release_names, sha256_file
from version_manager import parse_version, tag_for

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True, encoding="utf-8").strip()


def output(values: dict) -> None:
    for key, value in values.items():
        line = f"{key}={value}"
        print(line)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write(line + "\n")


def resolve() -> dict:
    tagged = os.environ.get("GITHUB_REF_TYPE") == "tag"
    supplied = os.environ.get("IN_VERSION", "").strip()
    version = (
        (os.environ["GITHUB_REF_NAME"][1:])
        if tagged
        else supplied or (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    )
    _, channel = parse_version(version)
    target = "all" if tagged else os.environ.get("IN_TARGET", "all")
    if target not in ("all", "7z", "full", "app-core", "setup"):
        raise ValueError(f"Unsupported release target: {target}")
    publish = tagged or os.environ.get("IN_PUBLISH") == "true"
    tag = tag_for(version)
    if publish:
        if target != "all":
            raise ValueError("Publishing requires target=all")
        if version != (ROOT / "VERSION").read_text(encoding="utf-8").strip():
            raise ValueError("Publication version differs from committed VERSION")
        head = command("git", "rev-parse", "HEAD")
        if command("git", "rev-parse", f"refs/tags/{tag}^{{commit}}") != head:
            raise ValueError("Release tag must exist and point to this exact commit")
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, "origin/main"], cwd=ROOT, check=True
        )
    return {
        "version": version,
        "tag": tag,
        "target": target,
        "channel": channel,
        "publish": str(publish and channel != "beta").lower(),
    }


def validate_result(path: Path, target: str | None = None, version: str | None = None) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    if target and result["target"] != target:
        raise ValueError("Build result target mismatch")
    if version and result["version"] != version:
        raise ValueError("Build result version mismatch")
    expected = set(release_names(result["target"], result["version"]).values())
    if {item["name"] for item in result["artifacts"]} != expected:
        raise ValueError("Artifact set differs from target contract")
    for artifact in result["artifacts"]:
        file = Path(artifact["path"])
        if file.name != artifact["name"] or not file.is_file():
            raise ValueError("Artifact missing or incorrectly named")
        if file.stat().st_size != artifact["size"] or sha256_file(file) != artifact["sha256"]:
            raise ValueError(f"Artifact hash/size mismatch: {file.name}")
    if "update-manifest.json" in expected:
        manifest_file = next(
            Path(a["path"]) for a in result["artifacts"] if a["name"] == "update-manifest.json"
        )
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest["app_version"] != result["version"] or manifest["release_tag"] != tag_for(
            result["version"]
        ):
            raise ValueError("Update manifest version/tag mismatch")
        core = manifest["components"]["app-core"]
        archive = next(a for a in result["artifacts"] if a["name"].endswith("-app-core.7z"))
        if core["sha256"] != archive["sha256"] or core["size"] != archive["size"]:
            raise ValueError("Update manifest payload hash/size mismatch")
    return result


def stage() -> None:
    result = validate_result(
        ROOT / "build/latest-result.json",
        os.environ.get("BUILD_TARGET"),
        os.environ.get("BUILD_VERSION"),
    )
    destination = ROOT / "build" / "upload"
    destination.mkdir(exist_ok=False)
    for artifact in result["artifacts"]:
        shutil.copy2(artifact["path"], destination / artifact["name"])
    output({"artifacts": str(destination), "workspace": result["workspace"]})


def verify_downloads(repository: str, tag: str, result: dict, *, public: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="release_verify_") as tmp:
        if public:
            import urllib.request
            from urllib.parse import quote

            remote = json.loads(command("gh", "api", f"repos/{repository}/releases/tags/{tag}"))
            _, channel = parse_version(result["version"])
            if remote.get("prerelease") is not (channel == "rc"):
                raise ValueError("Public release prerelease flag differs from version channel")
            if remote["draft"] or {a["name"] for a in remote["assets"]} != {
                a["name"] for a in result["artifacts"]
            }:
                raise ValueError("Public asset set mismatch")
            for artifact in result["artifacts"]:
                url = f"https://github.com/{repository}/releases/download/{tag}/{quote(artifact['name'])}"
                with (
                    urllib.request.urlopen(url, timeout=120) as response,
                    (Path(tmp) / artifact["name"]).open("wb") as stream,
                ):
                    shutil.copyfileobj(response, stream)
        else:
            command("gh", "release", "download", tag, "--repo", repository, "--dir", tmp)
        actual = {p.name for p in Path(tmp).iterdir() if p.is_file()}
        if actual != {a["name"] for a in result["artifacts"]}:
            raise ValueError("Remote release asset set mismatch")
        for item in result["artifacts"]:
            downloaded = Path(tmp) / item["name"]
            if (
                sha256_file(downloaded) != item["sha256"]
                or downloaded.stat().st_size != item["size"]
            ):
                raise ValueError(f"Remote asset mismatch: {item['name']}")


def publish() -> None:
    result = validate_result(ROOT / "build/latest-result.json", "all")
    if result["dirty"] or result["component_policy"] != "latest":
        raise ValueError("Publication requires clean source and a newly resolved latest snapshot")
    if result["git_commit"] != command("git", "rev-parse", "HEAD"):
        raise ValueError("Build commit differs from publishing commit")
    policy = resolve()
    if policy["publish"] != "true" or policy["version"] != result["version"]:
        raise ValueError("Publication not authorized by release policy")
    repository = os.environ["GITHUB_REPOSITORY"]
    tag = policy["tag"]
    releases = json.loads(
        command("gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100")
    )
    existing = next((r for page in releases for r in page if r["tag_name"] == tag), None)
    if existing:
        if not existing["draft"]:
            raise ValueError("A public release already exists; use a new version")
        # A failed draft may be resumed only with identical complete bytes.
        verify_downloads(repository, tag, result)
    else:
        notes = ROOT / "build/release-notes.md"
        base = f"https://github.com/{repository}/releases/download/{tag}/FluentYTDL-{result['version']}-win64"
        manifest_path = next(
            Path(a["path"]) for a in result["artifacts"] if a["name"] == "update-manifest.json"
        )
        changelog = json.loads(manifest_path.read_text(encoding="utf-8")).get("changelog", "")
        channel_notice = (
            "这是 rc 预发布，供测试使用，不替代稳定版 Latest。客户端默认 stable 通道；在设置中确认风险并切换到 pre 后，可在应用内更新后续 rc 或正式版。切回 stable 不会降级。\n\n"
            if policy["channel"] == "rc"
            else ""
        )
        notes.write_text(
            channel_notice
            + f"[便携完整版]({base}-full.7z) · [安装向导]({base}-setup.exe)\n\n"
            + (str(changelog).strip() + "\n\n" if changelog else "")
            + "app-core.7z 和 update-manifest.json 供应用内更新使用。\n\n"
            "安装器支持简体中文与英文，默认仅为当前用户安装。卸载清除账号、配置和历史，保留下载成品。\n\n"
            f"构建提交：{result['git_commit']}。所有附加组件在本次构建时获取上游最新版本。\n",
            encoding="utf-8",
        )
        args = [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--verify-tag",
            "--draft",
            "--title",
            f"FluentYTDL {tag}",
            "--notes-file",
            str(notes),
        ]
        if policy["channel"] == "rc":
            args.append("--prerelease")
        command(*args, *(a["path"] for a in result["artifacts"]))
        verify_downloads(repository, tag, result)
    command(
        "gh",
        "release",
        "edit",
        tag,
        "--repo",
        repository,
        "--draft=false",
        "--prerelease=" + str(policy["channel"] == "rc").lower(),
        "--latest=" + str(policy["channel"] == "stable").lower(),
    )
    verify_downloads(repository, tag, result, public=True)
    if policy["channel"] == "stable":
        import urllib.request

        url = f"https://github.com/{repository}/releases/latest/download/update-manifest.json"
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read()
        import hashlib

        expected = next(a for a in result["artifacts"] if a["name"] == "update-manifest.json")
        if (
            json.loads(payload)["release_tag"] != tag
            or len(payload) != expected["size"]
            or hashlib.sha256(payload).hexdigest() != expected["sha256"]
        ):
            raise ValueError("Public latest updater endpoint does not point to this release")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["resolve", "stage", "publish", "verify"])
    action = parser.parse_args().action
    if action == "resolve":
        output(resolve())
    elif action == "stage":
        stage()
    elif action == "publish":
        publish()
    else:
        validate_result(ROOT / "build/latest-result.json")
