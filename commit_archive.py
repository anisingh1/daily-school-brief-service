"""
Commits and pushes changes under data/ (the portal message archive,
cursor, and downloaded PDFs). The daily_brief.py pipeline calls this
when --commit-archive is explicitly enabled.

The git plumbing lives in one tested, reusable script.

A push failure (e.g. a rejected non-fast-forward push because another
run or a human pushed to data/ in between) is expected and recoverable -
it's reported as a warning, not raised as an error: it only affects
whether the next run starts from today's
updated archive, not today's brief. Do NOT force-push, rebase, or
otherwise try to resolve a rejected push here.

USAGE:
    python commit_archive.py
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent
COMMIT_MESSAGE = "Update portal archive"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True)


def commit_and_push() -> None:
    add_result = _run(["git", "add", "data/"])
    if add_result.returncode != 0:
        raise RuntimeError(f"git add failed: {add_result.stderr.strip()}")

    if _run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        commit_result = _run(["git", "commit", "-m", COMMIT_MESSAGE])
        if commit_result.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_result.stderr.strip()}")
        print("Committed data/ changes.")
    else:
        print("No data/ changes to commit.")

    push_result = _run(["git", "push"])
    if push_result.returncode != 0:
        print(
            "WARNING: git push failed - this run's archive update wasn't "
            "pushed (a later run will retry from wherever the remote "
            f"actually is). Details: {push_result.stderr.strip()}"
        )
    else:
        print("Pushed to origin.")


if __name__ == "__main__":
    commit_and_push()
