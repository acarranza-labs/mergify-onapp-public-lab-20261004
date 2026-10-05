"""Fixed, self-cleaning GitHub-only capability probe for the owned R3 repo.

Install at .github/scripts/bounty-notes-capability.py on R3 main, paired with
bounty-notes-capability.yml. This code is only for workflow_dispatch on main.
It never calls Mergify, prints credentials, or mutates a branch other than the
guarded no-tree-change update attempted against main. A successful main update
is a NO-GO requiring owner review; it does not continue to Git notes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request


REPO = "acarranza-labs/mergify-onapp-public-lab-20261004"
REPO_ID = "1404856005"
HISTORICAL_TARGET = "2199312093f17f8920df1005da1adc3fe90f04fd"
NOTE_PREFIX = "refs/notes/mergify/bounty-capability-20261005-"
SHA = re.compile(r"[0-9a-f]{40}\Z")
OWN_NOTE = re.compile(r"refs/notes/mergify/bounty-capability-20261005-[1-9][0-9]*-[1-9][0-9]*\Z")
API_BASE = f"repos/{REPO}"


class ProbeError(RuntimeError):
    pass


class ApiError(ProbeError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"github_api_http_{code}")


def api(method: str, path: str, body: dict | None = None) -> tuple[int, object | None]:
    if method not in {"GET", "POST", "PATCH"} or not (
        path == API_BASE or path.startswith(API_BASE + "/")
    ):
        raise ProbeError("github_api_path_outside_owned_r3")
    token = os.environ["LAB_GITHUB_TOKEN"]
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Never print the response body or request headers in a public run log.
        return error.code, None


def required(method: str, path: str, body: dict | None = None, status: int = 200) -> dict:
    code, result = api(method, path, body)
    if code != status or not isinstance(result, dict):
        raise ApiError(code)
    return result


def main_ref() -> str:
    result = required("GET", f"{API_BASE}/git/ref/heads/main")
    actual = (result.get("object") or {}).get("sha")
    if not isinstance(actual, str) or not SHA.fullmatch(actual):
        raise ProbeError("invalid_main_ref_response")
    return actual


def git(*args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False, timeout=30,
    )
    if ok and result.returncode:
        raise ProbeError(f"git_{args[0]}_exit_{result.returncode}")
    return result


def remote_ref(ref: str) -> str | None:
    if not OWN_NOTE.fullmatch(ref):
        raise ProbeError("notes_ref_outside_fixed_namespace")
    result = git("ls-remote", "--refs", "origin", ref)
    lines = [line.split("\t", 1) for line in result.stdout.splitlines() if line.strip()]
    matches = [sha for sha, name in lines if name == ref]
    if len(matches) > 1 or any(not SHA.fullmatch(sha) for sha in matches):
        raise ProbeError("ambiguous_remote_note_ref")
    return matches[0] if matches else None


def guard() -> tuple[str, str, str]:
    mode = os.environ.get("LAB_MODE", "")
    expected = os.environ.get("LAB_EXPECTED_MAIN", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    remote_url = git("remote", "get-url", "origin").stdout.strip()
    if (
        mode not in {"probe", "cleanup"}
        or not SHA.fullmatch(expected)
        or not run_id.isdecimal() or not attempt.isdecimal()
        or int(run_id) <= 0 or int(attempt) <= 0
        or not os.environ.get("LAB_GITHUB_TOKEN")
        or os.environ.get("GITHUB_REPOSITORY") != REPO
        or os.environ.get("GITHUB_REPOSITORY_ID") != REPO_ID
        or os.environ.get("GITHUB_SERVER_URL") != "https://github.com"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_REF_PROTECTED") != "true"
        or os.environ.get("GITHUB_ACTOR") != "acarranza-labs"
        or os.environ.get("GITHUB_TRIGGERING_ACTOR") != "acarranza-labs"
        or os.environ.get("GITHUB_SHA") != expected
        or remote_url not in {f"https://github.com/{REPO}", f"https://github.com/{REPO}.git"}
        or git("rev-parse", "HEAD").stdout.strip() != expected
        or main_ref() != expected
    ):
        raise ProbeError("dispatch_or_checkout_guard_failed")
    repo = required("GET", API_BASE)
    if (
        str(repo.get("id")) != REPO_ID or repo.get("full_name") != REPO
        or repo.get("private") is not False or repo.get("fork") is not False
        or repo.get("default_branch") != "main" or repo.get("archived") is not False
    ):
        raise ProbeError("repository_identity_guard_failed")
    return mode, expected, f"{run_id}-{attempt}"


def main_denial(expected: str, run_attempt: str, result: dict) -> bool:
    original = required("GET", f"{API_BASE}/git/commits/{expected}")
    tree = (original.get("tree") or {}).get("sha")
    if not isinstance(tree, str) or not SHA.fullmatch(tree):
        raise ProbeError("invalid_main_tree")
    # A new fast-forward commit with the *same* tree tests the branch rule
    # without adding or changing a file if GitHub unexpectedly accepts it.
    candidate = required("POST", f"{API_BASE}/git/commits", {
        "message": f"Bounty R3: no-tree-change main write control {run_attempt}",
        "tree": tree,
        "parents": [expected],
    }, status=201)
    attempted_sha = candidate.get("sha")
    if not isinstance(attempted_sha, str) or not SHA.fullmatch(attempted_sha):
        raise ProbeError("invalid_control_commit")
    result["main_control_commit"] = attempted_sha
    result["main_control_tree"] = tree
    if main_ref() != expected:
        raise ProbeError("main_moved_before_control_update")
    code, _ = api("PATCH", f"{API_BASE}/git/refs/heads/main", {
        "sha": attempted_sha,
        "force": False,
    })
    after = main_ref()
    result["main_update_http"] = code
    result["main_after_control"] = after
    if code in {403, 422} and after == expected:
        return True
    if code == 200 and after == attempted_sha:
        result["verdict"] = "NO_GO_MAIN_WRITABLE"
        result["cleanup_warning"] = "Main points to a new commit with the original tree; owner must review exact rollback."
        return False
    result["verdict"] = "INCONCLUSIVE_MAIN_CONTROL"
    return False


def clean_exact_note(ref: str, expected_sha: str, result: dict) -> bool:
    if not OWN_NOTE.fullmatch(ref) or not SHA.fullmatch(expected_sha):
        raise ProbeError("cleanup_identity_guard_failed")
    current = remote_ref(ref)
    result["note_ref_before_cleanup"] = current
    if current is None:
        result["note_cleanup"] = "already_absent"
        return True
    if current != expected_sha:
        result["note_cleanup"] = "blocked_remote_ref_changed"
        return False
    # Explicit SHA lease makes deletion atomic for this one note ref only.
    pushed = git(
        "push", f"--force-with-lease={ref}:{expected_sha}",
        "origin", f":{ref}", ok=False,
    )
    if pushed.returncode:
        result["note_cleanup"] = f"delete_exit_{pushed.returncode}"
        return False
    if remote_ref(ref) is not None:
        result["note_cleanup"] = "delete_not_confirmed"
        return False
    result["note_cleanup"] = "deleted_exact_ref"
    return True


def probe(expected: str, run_attempt: str, result: dict) -> None:
    if not main_denial(expected, run_attempt, result):
        return
    if main_ref() != expected:
        raise ProbeError("main_moved_after_denial")
    note_ref = NOTE_PREFIX + run_attempt
    if not OWN_NOTE.fullmatch(note_ref) or remote_ref(note_ref) is not None:
        raise ProbeError("notes_ref_already_exists_or_invalid")
    if git("cat-file", "-t", HISTORICAL_TARGET).stdout.strip() != "commit":
        raise ProbeError("historical_note_target_missing")
    marker = f"bounty-capability-20261005 run {run_attempt}; inert historical target"
    note_short = note_ref.removeprefix("refs/notes/")
    git(
        "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "notes", f"--ref={note_short}", "add", "-m", marker, HISTORICAL_TARGET,
    )
    local_sha = git("rev-parse", note_ref).stdout.strip()
    if not SHA.fullmatch(local_sha):
        raise ProbeError("invalid_local_note_commit")
    result["note_ref"] = note_ref
    result["note_target"] = HISTORICAL_TARGET
    result["note_commit"] = local_sha
    try:
        pushed = git("push", "origin", f"{note_ref}:{note_ref}", ok=False)
        result["note_push_exit"] = pushed.returncode
        if pushed.returncode:
            # A transport error after the server accepted the ref is not a
            # permission denial. The finally block still removes our ref.
            result["verdict"] = (
                "INCONCLUSIVE_NOTES_PUSH_TRANSPORT"
                if remote_ref(note_ref) == local_sha else "NO_GO_NOTES_PUSH_DENIED"
            )
            return
        if remote_ref(note_ref) != local_sha or main_ref() != expected:
            result["verdict"] = "INCONCLUSIVE_NOTES_OR_MAIN_MOVED"
            return
        verify_ref = "refs/notes/verify-bounty-capability-20261005-" + run_attempt
        fetched = git("fetch", "--no-tags", "origin", f"{note_ref}:{verify_ref}", ok=False)
        result["note_verify_fetch_exit"] = fetched.returncode
        if fetched.returncode:
            result["verdict"] = "INCONCLUSIVE_NOTES_FETCH"
            return
        try:
            verified = git(
                "notes", f"--ref={verify_ref}", "show", HISTORICAL_TARGET,
            ).stdout.strip()
            result["note_content_verified"] = verified == marker
            result["verdict"] = "GO_CAPABILITY" if verified == marker else "INCONCLUSIVE_NOTES_CONTENT"
        finally:
            git("update-ref", "-d", verify_ref, ok=False)
    finally:
        try:
            cleaned = clean_exact_note(note_ref, local_sha, result)
        except (ProbeError, subprocess.TimeoutExpired):
            cleaned = False
            result["note_cleanup"] = "unverified_api_or_git_error"
        git("update-ref", "-d", note_ref, ok=False)
        if not cleaned:
            result["verdict"] = "INCONCLUSIVE_NOTES_CLEANUP_PENDING"
        elif main_ref() != expected:
            result["verdict"] = "INCONCLUSIVE_MAIN_MOVED_AFTER_NOTES"


def cleanup(expected: str, result: dict) -> None:
    ref = os.environ.get("LAB_CLEANUP_REF", "")
    sha = os.environ.get("LAB_CLEANUP_SHA", "")
    if not OWN_NOTE.fullmatch(ref) or not SHA.fullmatch(sha):
        raise ProbeError("cleanup_inputs_invalid")
    result["note_ref"] = ref
    result["note_commit"] = sha
    done = clean_exact_note(ref, sha, result)
    result["verdict"] = "CLEANUP_COMPLETE" if done and main_ref() == expected else "CLEANUP_PENDING"


def write_result(result: dict) -> None:
    run_id = os.environ.get("GITHUB_RUN_ID", "unknown")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "unknown")
    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    output = temp / f"bounty-notes-capability-{run_id}-{attempt}.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(output, 0o600)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def main() -> int:
    result: dict = {
        "schema": 1,
        "repo": REPO,
        "repo_id": int(REPO_ID),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "mode": os.environ.get("LAB_MODE"),
        "expected_main_sha": (
            os.environ.get("LAB_EXPECTED_MAIN")
            if SHA.fullmatch(os.environ.get("LAB_EXPECTED_MAIN", "")) else None
        ),
        "verdict": "INCONCLUSIVE",
    }
    try:
        mode, expected, run_attempt = guard()
        if mode == "probe":
            probe(expected, run_attempt, result)
        else:
            cleanup(expected, result)
    except ApiError as error:
        result["error"] = str(error)
    except subprocess.TimeoutExpired:
        result["error"] = "git_timeout"
    except ProbeError as error:
        # All ProbeError text is fixed in this file. Never print an HTTP body,
        # git stderr, environment dump, or exception containing a token.
        result["error"] = str(error)
    except (OSError, KeyError, ValueError):
        result["error"] = "runtime_failure"
    if "error" in result and result["verdict"] in {"GO_CAPABILITY", "CLEANUP_COMPLETE"}:
        result["verdict"] = "INCONCLUSIVE_RUNTIME_ERROR"
    write_result(result)
    return 0 if result["verdict"] in {"GO_CAPABILITY", "CLEANUP_COMPLETE"} else 1


if __name__ == "__main__":
    sys.exit(main())
