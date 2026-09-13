"""Local fault injection; never contacts or mutates a real GitHub repository."""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_pipeline as pipeline


@pytest.mark.parametrize(
    "scenario",
    [
        "stable",
        "rc",
        "resume-rc",
        "bad-rc-flag",
        "resume",
        "already-public",
        "missing-draft-asset",
        "bad-public-bytes",
        "bad-latest",
    ],
)
def test_draft_protocol(tmp_path, monkeypatch, scenario):
    is_rc = scenario in ("rc", "resume-rc", "bad-rc-flag")
    version = "3.7.1-rc.1" if is_rc else "3.7.1"
    tag = "v" + version
    (tmp_path / "build").mkdir()
    (tmp_path / "VERSION").write_text(version)
    files = {}
    for name in pipeline.release_names("all", version).values():
        files[name] = name.encode()
    core_name = next(n for n in files if n.endswith("-app-core.7z"))
    files["update-manifest.json"] = json.dumps(
        {
            "app_version": version,
            "release_tag": tag,
            "components": {
                "app-core": {
                    "size": len(files[core_name]),
                    "sha256": hashlib.sha256(files[core_name]).hexdigest(),
                }
            },
        }
    ).encode()
    artifacts = []
    for name, payload in files.items():
        path = tmp_path / name
        path.write_bytes(payload)
        artifacts.append(
            {
                "name": name,
                "path": str(path),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    result = {
        "target": "all",
        "version": version,
        "git_commit": "fixture-commit",
        "dirty": False,
        "component_policy": "latest",
        "artifacts": artifacts,
    }
    (tmp_path / "build/latest-result.json").write_text(json.dumps(result))
    remote = {
        "exists": scenario in ("resume", "resume-rc", "already-public"),
        "draft": scenario != "already-public",
        "prerelease": False,
        "files": dict(files),
    }
    operations = []

    def command(*args):
        if args[0] == "git":
            return "fixture-commit"
        if args[:4] == ("gh", "api", "--paginate", "--slurp"):
            return json.dumps(
                [[{"tag_name": tag, "draft": remote["draft"]}] if remote["exists"] else []]
            )
        if args[:3] == ("gh", "release", "create"):
            operations.append("create")
            assert "--draft" in args and "--verify-tag" in args
            assert ("--prerelease" in args) == is_rc
            if is_rc:
                notes = Path(args[args.index("--notes-file") + 1]).read_text(encoding="utf-8")
                assert "pre" in notes and "应用内更新" in notes
                assert "不支持应用内自动更新" not in notes
            remote["exists"] = True
            if scenario == "missing-draft-asset":
                remote["files"].pop(core_name)
            return ""
        if args[:3] == ("gh", "release", "download"):
            operations.append("draft-download")
            dest = Path(args[args.index("--dir") + 1])
            for name, payload in remote["files"].items():
                (dest / name).write_bytes(payload)
            return ""
        if args[:3] == ("gh", "release", "edit"):
            operations.append("publish")
            remote["draft"] = False
            assert "--latest=" + ("false" if is_rc else "true") in args
            assert "--prerelease=" + str(is_rc).lower() in args
            remote["prerelease"] = is_rc and scenario != "bad-rc-flag"
            return ""
        if args[:2] == ("gh", "api"):
            return json.dumps(
                {
                    "draft": remote["draft"],
                    "prerelease": remote["prerelease"],
                    "assets": [{"name": name} for name in remote["files"]],
                }
            )
        raise AssertionError(args)

    def urlopen(url, **kwargs):
        operations.append("public-download")
        if "/latest/" in url and scenario == "bad-latest":
            return io.BytesIO(json.dumps({"release_tag": tag}).encode())
        payload = remote["files"][url.rsplit("/", 1)[-1]]
        if scenario == "bad-public-bytes":
            payload += b"corrupt"
        return io.BytesIO(payload)

    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "command", command)
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    for key, value in {
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REF_NAME": tag,
        "GITHUB_REPOSITORY": "fixture/repository",
    }.items():
        monkeypatch.setenv(key, value)
    if scenario in (
        "already-public",
        "missing-draft-asset",
        "bad-public-bytes",
        "bad-latest",
        "bad-rc-flag",
    ):
        with pytest.raises(ValueError):
            pipeline.publish()
        if scenario in ("bad-public-bytes", "bad-latest", "bad-rc-flag"):
            assert remote["exists"] and not remote["draft"]
        else:
            assert "publish" not in operations
    else:
        pipeline.publish()
        assert (
            operations.index("draft-download")
            < operations.index("publish")
            < operations.index("public-download")
        )
        assert ("create" not in operations) == (scenario in ("resume", "resume-rc"))


@pytest.mark.parametrize(
    "scenario", ["beta", "wrong-version", "wrong-tag-commit", "outside-main", "partial-publication"]
)
def test_release_policy(tmp_path, monkeypatch, scenario):
    version = "3.7.1-beta.1" if scenario == "beta" else "3.7.1"
    (tmp_path / "VERSION").write_text(version)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    for key, value in {
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REF_NAME": "v" + version,
        "IN_VERSION": "",
        "IN_TARGET": "all",
        "IN_PUBLISH": "false",
    }.items():
        monkeypatch.setenv(key, value)
    if scenario == "wrong-version":
        monkeypatch.setenv("GITHUB_REF_NAME", "v3.7.2")
    if scenario == "partial-publication":
        monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
        monkeypatch.setenv("IN_PUBLISH", "true")
        monkeypatch.setenv("IN_TARGET", "setup")

    def command(*args):
        return "other" if scenario == "wrong-tag-commit" and "refs/tags/" in args[-1] else "head"

    def ancestry(*args, **kwargs):
        if scenario == "outside-main":
            raise pipeline.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(pipeline, "command", command)
    monkeypatch.setattr(pipeline.subprocess, "run", ancestry)
    if scenario == "beta":
        result = pipeline.resolve()
        assert result["channel"] == "beta" and result["publish"] == "false"
    else:
        with pytest.raises((ValueError, pipeline.subprocess.CalledProcessError)):
            pipeline.resolve()
