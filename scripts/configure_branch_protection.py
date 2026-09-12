"""Enable protection only after the new required check has landed and passed on main."""

from __future__ import annotations

import argparse
import json
import subprocess

from release_pipeline import command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="SakuraForgot/FluentYTDL")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    branch = json.loads(command("gh", "api", f"repos/{args.repo}/branches/main"))
    sha = branch["commit"]["sha"]
    checks = json.loads(command("gh", "api", f"repos/{args.repo}/commits/{sha}/check-runs"))
    ready = any(
        check["name"] == "Required checks" and check["conclusion"] == "success"
        for check in checks["check_runs"]
    )
    if not ready:
        raise SystemExit("Protection not changed: main has no successful 'Required checks' run yet")
    body = {
        "required_status_checks": {"strict": True, "contexts": ["Required checks"]},
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "required_approving_review_count": 0,
        },
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
    print(json.dumps(body, indent=2))
    if args.apply:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PUT",
                f"repos/{args.repo}/branches/main/protection",
                "--input",
                "-",
            ],
            input=json.dumps(body),
            text=True,
            check=True,
        )


if __name__ == "__main__":
    main()
