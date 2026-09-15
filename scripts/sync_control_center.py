"""Refresh cached metadata after release verification; never print credentials."""

import os
import sys

import requests


def main():
    token = os.environ.get("CONTROL_CENTER_SYNC_TOKEN", "")
    if not token:
        print(
            "::warning::CONTROL_CENTER_SYNC_TOKEN is not configured; scheduled Worker sync remains available."
        )
        return 0
    try:
        response = requests.post(
            "https://fluentytdl.sakuraforgot.com/internal/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={"github_token": os.environ["GH_TOKEN"]},
            timeout=(10, 180),
        )
        response.raise_for_status()
        outcomes = response.json()
        failed = [key for key, status in outcomes.items() if status != "ok"]
        failed.extend(key for key in ("app:stable", "app:pre") if key not in outcomes)
        print("Control center refresh:", "partial failure" if failed else "complete")
        if failed:
            print("Failed channels:", ", ".join(failed))
            return 1
        return 0
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        code = "unknown"
        if exc.response is not None:
            try:
                candidate = exc.response.json().get("error", "")
                if isinstance(candidate, str) and candidate.replace("_", "").isalnum():
                    code = candidate[:80]
            except (ValueError, AttributeError):
                pass
        print(f"::error::Control center synchronization failed: HTTP {status}, code={code}")
        return 1
    except (requests.RequestException, ValueError, KeyError):
        print("::error::Control center synchronization failed; inspect Worker sync status.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
