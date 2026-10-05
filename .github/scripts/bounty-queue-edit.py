"""Fixed Actions-token probe for the owned R3 queue branch only.

This script is installed on R3 main and invoked solely by workflow_dispatch.
No token or API error body is printed. It never force-pushes or deletes refs.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request


REPO = "acarranza-labs/mergify-onapp-public-lab-20261004"
REPO_ID = 1404856005
EXTRA = "bounty-queue-external-20261005.txt"
BATCH_PREFIX = "mergify/merge-queue/"
SHA = re.compile(r"[0-9a-f]{40}\Z")
token = os.environ.get("GH_TOKEN", "")


class ApiError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"GitHub API HTTP {code}")


def api(method, path, body=None, missing_ok=False):
    if not path.startswith(f"repos/{REPO}/"):
        raise RuntimeError("Path outside fixed owned R3")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        # The response body is deliberately discarded: public run logs must
        # never contain a token or unrelated repository data.
        raise ApiError(error.code) from None


def ref(branch):
    return api("GET", f"repos/{REPO}/git/ref/heads/{branch}")["object"]["sha"]


def commit(parent, path, text, message):
    original = api("GET", f"repos/{REPO}/git/commits/{parent}")
    blob = api("POST", f"repos/{REPO}/git/blobs", {
        "content": base64.b64encode(text.encode()).decode(), "encoding": "base64"})["sha"]
    tree = api("POST", f"repos/{REPO}/git/trees", {
        "base_tree": original["tree"]["sha"],
        "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob}]})["sha"]
    return api("POST", f"repos/{REPO}/git/commits", {
        "message": message, "tree": tree, "parents": [parent]})["sha"]


def update(branch, new_sha):
    return api("PATCH", f"repos/{REPO}/git/refs/heads/{branch}",
               {"sha": new_sha, "force": False})


def main_denial(expected):
    if ref("main") != expected:
        raise RuntimeError("Main moved before the direct-write control")
    old_tree = api("GET", f"repos/{REPO}/git/commits/{expected}")["tree"]["sha"]
    attempted = commit(expected, "bounty-queue-main-denial-20261005.txt",
                       "Inert main-denial control for owned R3.\n",
                       "Bounty lab: test Actions-token main write denial")
    try:
        update("main", attempted)
    except ApiError as error:
        if error.code not in (403, 409, 422):
            raise
        if ref("main") != expected:
            raise RuntimeError("Main moved during denied direct-write control")
        print(json.dumps({"main_write": "denied", "http_status": error.code,
                          "main_sha": expected, "run_id": os.environ["GITHUB_RUN_ID"]}))
        return

    # Unexpectedly writable: reconstruct the exact old tree in a normal
    # forward commit. The job aborts even if the compensating write succeeds.
    if ref("main") != attempted:
        raise RuntimeError("UNEXPECTED_MAIN_WRITE and main moved; manual recovery required")
    undo = api("POST", f"repos/{REPO}/git/commits", {
        "message": "Bounty lab: undo unexpected Actions-token main write",
        "tree": old_tree, "parents": [attempted]})["sha"]
    update("main", undo)
    print(json.dumps({"main_write": "UNEXPECTED_ALLOWED_RESTORED_TREE",
                      "old_sha": expected, "attempt_sha": attempted,
                      "undo_sha": undo, "run_id": os.environ["GITHUB_RUN_ID"]}))
    raise RuntimeError("Ruleset did not block the lower principal; stop experiment")


def edit_batch(mode, main_sha):
    branch = os.environ.get("LAB_BATCH_BRANCH", "")
    before = os.environ.get("LAB_EXPECTED_BATCH", "")
    pr_string = os.environ.get("LAB_BATCH_PR", "")
    if (not branch.startswith(BATCH_PREFIX)
            or not re.fullmatch(r"[a-zA-Z0-9/_-]+", branch)
            or not SHA.fullmatch(before) or not pr_string.isdecimal()):
        raise RuntimeError("Invalid pinned batch ref, SHA, or PR number")
    pr_number = int(pr_string)
    if not 1 <= pr_number <= 1000000:
        raise RuntimeError("Batch PR outside expected range")
    pull = api("GET", f"repos/{REPO}/pulls/{pr_number}")
    if (pull["state"] != "open" or pull["base"]["ref"] != "main"
            or pull["head"]["ref"] != branch
            or pull["head"]["sha"] != before
            or pull["head"]["repo"]["id"] != REPO_ID
            or pull["user"]["login"] != "mergify[bot]"
            or not pull["title"].startswith("merge queue:")):
        raise RuntimeError("PR is not the pinned Mergify batch")
    if ref("main") != main_sha or ref(branch) != before:
        raise RuntimeError("Main or batch ref moved before external edit")
    if api("GET", f"repos/{REPO}/contents/{EXTRA}?ref={before}", missing_ok=True):
        raise RuntimeError("Extra canary already exists")

    after = commit(before, EXTRA,
                   f"Inert external batch edit for owned R3; run {os.environ['GITHUB_RUN_ID']}.\n",
                   "Bounty lab: externally edit one Mergify batch branch")
    if ref("main") != main_sha or ref(branch) != before:
        raise RuntimeError("Main or batch ref moved before fast-forward")
    update(branch, after)
    if ref(branch) != after:
        raise RuntimeError("Batch ref did not persist at the exact child SHA")
    print(json.dumps({"batch_edit": "fast_forward", "mode": mode,
                      "branch": branch, "before": before, "after": after,
                      "main_sha_before": main_sha,
                      "run_id": os.environ["GITHUB_RUN_ID"]}))


def main():
    mode = os.environ.get("LAB_MODE", "")
    main_sha = os.environ.get("LAB_EXPECTED_MAIN", "")
    if (mode not in {"probe", "edit"}
            or not SHA.fullmatch(main_sha) or not token
            or os.environ.get("GITHUB_REPOSITORY") != REPO
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_ACTOR") != "acarranza-labs"):
        raise RuntimeError("Unexpected dispatch identity, ref, or fixed input")
    main_denial(main_sha)
    if mode != "probe":
        edit_batch(mode, main_sha)


if __name__ == "__main__":
    main()
