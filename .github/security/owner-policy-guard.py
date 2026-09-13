"""Trusted-base policy guard. Candidate source is inspected, never executed."""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Any, Callable

SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PROTECTED = re.compile(
    r"(^|/)(\.gitleaksignore|\.gitleaks\.toml|\.semgrepignore)$|^\.github/(workflows|security)/"
)
# Assemble names so the guard's own source does not introduce a suppression marker.
MARKERS = ("git" + "leaks:allow", "no" + "semgrep", "no" + "sem")
SUPPRESSION = re.compile(
    "(" + "|".join(re.escape(value) for value in MARKERS) + r")([^a-zA-Z0-9_]|$)",
    re.IGNORECASE,
)
MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 20
MAX_COMMITS = 1000
SUPPORTED_BASE_BRANCHES = ("main", "feature/boiler-nr67d")


class MissingOwnerApproval(ValueError):
    """Complete validated status history contains no matching approval context."""


def positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid numeric identity")
    return value


def commit(value: object) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ValueError("Invalid commit identity")
    return value


def repository_identity(value: object, expected_name: str) -> tuple[int, int]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("full_name"), str)
        or value["full_name"].casefold() != expected_name.casefold()
    ):
        raise ValueError("Repository identity mismatch")
    owner = value.get("owner")
    if not isinstance(owner, dict) or owner.get("type") != "User":
        raise ValueError("Policy approval requires a user-owned repository")
    return positive_id(value.get("id")), positive_id(owner.get("id"))


def validate_pull(
    pull: dict[str, Any],
    repository: dict[str, Any],
    branch: dict[str, Any],
    expected_repo: str,
    expected_number: int,
    expected_head: str,
    expected_base: str,
    expected_branch: str,
) -> int:
    if expected_branch not in SUPPORTED_BASE_BRANCHES:
        raise ValueError("Unsupported base branch")
    commit(expected_head)
    commit(expected_base)
    repo_id, owner_id = repository_identity(repository, expected_repo)
    if (
        positive_id(pull.get("number")) != expected_number
        or pull.get("state") != "open"
    ):
        raise ValueError("Pull request is not the current open request")
    if branch.get("name") != expected_branch:
        raise ValueError("Unexpected base branch")
    if commit(branch["commit"]["sha"]) != expected_base:
        raise ValueError("Base branch advanced")
    for side, sha in (("head", expected_head), ("base", expected_base)):
        item = pull.get(side)
        if not isinstance(item, dict) or commit(item.get("sha")) != sha:
            raise ValueError("Pull request commit changed")
        if repository_identity(item.get("repo"), expected_repo) != (repo_id, owner_id):
            raise ValueError("Fork or repository identity mismatch")
    if pull["base"].get("ref") != expected_branch:
        raise ValueError("Pull request base branch mismatch")
    return owner_id


def collect_statuses(fetch: Callable[[int], object]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page in range(1, MAX_PAGES + 1):
        values = fetch(page)
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("Invalid status page")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Invalid status object")
            identity = positive_id(value.get("id"))
            if identity in seen:
                raise ValueError("Repeated status page or identity")
            seen.add(identity)
            rows.append(value)
        if len(values) < 100:
            return rows
    raise ValueError("Status pagination is incomplete")


def owner_approval(statuses: list[dict[str, Any]], owner_id: int, base_sha: str) -> str:
    positive_id(owner_id)
    commit(base_sha)
    matches: list[tuple[datetime.datetime, int, dict[str, Any]]] = []
    for status in statuses:
        context = status.get("context")
        if not isinstance(context, str) or not context or len(context) > 256:
            raise ValueError("Invalid status context")
        identity = positive_id(status.get("id"))
        creator = status.get("creator")
        if not isinstance(creator, dict):
            raise ValueError("Invalid status creator")
        positive_id(creator.get("id"))
        when = status.get("created_at")
        if (
            not isinstance(when, str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", when) is None
        ):
            raise ValueError("Invalid status timestamp")
        timestamp = datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ")
        if status.get("state") not in ("success", "pending", "failure", "error"):
            raise ValueError("Invalid status state")
        if context.casefold() == "owner-policy-approval":
            matches.append((timestamp, identity, status))
    if not matches:
        raise MissingOwnerApproval("Exact owner policy approval is absent")
    latest = max(matches, key=lambda item: (item[0], item[1]))[2]
    if latest["creator"]["id"] != owner_id or latest["state"] != "success":
        raise ValueError("Latest approval is not an owner success")
    description = latest.get("description")
    if (
        not isinstance(description, str)
        or re.fullmatch(r"v1 base=" + base_sha + r" approval=[0-9a-f]{64}", description)
        is None
    ):
        raise ValueError("Approval is not bound to the current base")
    return description.rsplit("=", 1)[1]


def policy_changes(paths: list[str], patch: str) -> bool:
    in_hunk = True
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+") and SUPPRESSION.search(line[1:]):
            raise ValueError(
                "Pull requests may not add inline scanner-suppression markers"
            )
    return any(PROTECTED.search(path.lstrip("\n")) is not None for path in paths)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, check=False)
    if result.returncode != 0 or len(result.stdout) > MAX_BYTES:
        raise ValueError("Bounded source observation failed")
    return result.stdout.decode("utf-8")


def policy_history(base: str, head: str) -> bool:
    """Inspect ordinary patches against every parent, including imported roots."""
    commit(base)
    commit(head)
    if git('rev-parse', '--is-shallow-repository') != 'false\n':
        raise ValueError('Complete source history is required')
    history = git('rev-list', '--parents', base + '..' + head)
    if history and not history.endswith('\n'):
        raise ValueError('Incomplete commit traversal')
    rows = history[:-1].split('\n') if history else []
    if len(rows) > MAX_COMMITS:
        raise ValueError('Commit traversal exceeds limit')
    commits: dict[str, list[str]] = {}
    for row in rows:
        tokens = row.split(' ')
        for token in tokens:
            commit(token)
        current, parents = tokens[0], tokens[1:]
        if current in commits or current in parents or len(set(parents)) != len(parents):
            raise ValueError('Invalid commit traversal')
        commits[current] = parents
    if base in commits or (base == head and commits) or (base != head and head not in commits):
        raise ValueError('Incomplete commit traversal')
    protected = False
    remaining = MAX_BYTES - len(history.encode('utf-8'))

    def inspect(prefix: tuple[str, ...], revisions: tuple[str, ...]) -> None:
        nonlocal protected, remaining
        names = git(*prefix, '--name-only', '-z', '--no-renames', *revisions, '--')
        patch = git(*prefix, '--patch', '--unified=0', '--no-ext-diff', '--no-textconv',
                    '--no-renames', *revisions, '--')
        remaining -= len(names.encode('utf-8')) + len(patch.encode('utf-8'))
        if remaining < 0 or (names and not names.endswith('\0')):
            raise ValueError('Incomplete or excessive policy observation')
        paths = names[:-1].split('\0') if names else []
        if any(not path for path in paths):
            raise ValueError('Invalid policy path observation')
        # Evaluate every patch even after a protected path has been observed.
        changed = policy_changes(paths, patch)
        protected = protected or changed

    for current, parents in commits.items():
        if parents:
            for parent in parents:
                inspect(('diff',), (parent, current))
        else:
            inspect(('diff-tree', '--root', '--no-commit-id', '-r'), (current,))
    inspect(('diff',), (base, head))
    return protected


def api(path: str, token: str) -> Any:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
        ) -> Any:
            return None

    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": "Bearer " + token,
            "User-Agent": "navien-owner-policy-guard",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError("Policy API request failed")
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Policy API response exceeds limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate policy API field")
            value[key] = item
        return value

    def invalid_constant(_value: str) -> None:
        raise ValueError("Nonfinite policy API value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def main() -> int:
    try:
        base, head = commit(os.environ["BASE_SHA"]), commit(os.environ["HEAD_SHA"])
        name = os.environ["GITHUB_REPOSITORY"]
        if len(name) > 256 or REPOSITORY.fullmatch(name) is None:
            raise ValueError("Invalid repository name")
        number = int(os.environ["PR_NUMBER"])
        positive_id(number)
        if number > 2**63 - 1:
            raise ValueError("Pull request number exceeds bound")
        branch_name = os.environ["EXPECTED_BASE_BRANCH"]
        if branch_name not in SUPPORTED_BASE_BRANCHES:
            raise ValueError("Invalid base branch")
        git("cat-file", "-e", base + "^{commit}")
        git("fetch", "--no-tags", "origin", f"refs/pull/{number}/head")
        if git("rev-parse", "FETCH_HEAD").strip() != head:
            raise ValueError("Fetched pull request head differs from event head")
        if policy_history(base, head):
            token = os.environ["GITHUB_TOKEN"]
            if not token:
                raise ValueError("Read-only token is missing")
            repository = api("/repos/" + name, token)
            pull = api(f"/repos/{name}/pulls/{number}", token)
            # Encode the branch as one path component, never an API selector.
            from urllib.parse import quote

            branch = api(
                "/repos/" + name + "/branches/" + quote(branch_name, safe=""), token
            )
            owner_id = validate_pull(
                pull, repository, branch, name, number, head, base, branch_name
            )
            statuses = collect_statuses(
                lambda page: api(
                    f"/repos/{name}/commits/{head}/statuses?per_page=100&page={page}",
                    token,
                )
            )
            try:
                owner_approval(statuses, owner_id, base)
            except MissingOwnerApproval:
                # Only trusted base code emits this line. Consumers must authenticate the
                # complete fixed job and guard step; a matching log substring is not proof.
                diagnosis = {
                    "schema": "owner-policy-guard-diagnosis-v1",
                    "reason": "MISSING_OWNER_APPROVAL",
                    "repository": name,
                    "pr_number": number,
                    "base_sha": base,
                    "head_sha": head,
                }
                print(
                    "OWNER_POLICY_GUARD_DIAGNOSIS "
                    + json.dumps(diagnosis, sort_keys=True, separators=(",", ":"))
                )
                raise
        print(
            "Trusted-base policy guard passed; this does not establish CI or source acceptance."
        )
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
        print(
            "Policy guard rejected missing, changed, invalid or unauthorized evidence.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
