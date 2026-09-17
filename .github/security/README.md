# Trusted owner policy maintenance

The `security-policy` workflow's `policy-guard` job runs only trusted default-branch code under
`pull_request_target`. Checkout remains pinned, without persistent credentials or a PR-head ref.
The Python guard fetches the exact PR head, checks it against the event, and inspects commit
history plus the aggregate diff without executing candidate code. Added inline scanner suppression
markers are rejected unconditionally, even when later reverted or when policy maintenance has
owner approval. Workflow and guard/security paths and scanner-ignore/config files are protected.

Protected changes require the newest case-insensitive `owner-policy-approval` status on that exact
HEAD, selected across every creator. It must be `success` from the repository owner's numeric
user ID. A newer foreign, pending, failed or revoked status blocks an older approval. The repository
must be user-owned; both PR sides must belong to it; PR/head/base and the selected current base
branch must match. The workflow supplies the event PR base ref; the trusted guard independently
allows exactly `main` and `feature/boiler-nr67d`, then verifies the live branch name/SHA and PR
base ref/SHA. The selected branch may differ from `repository.default_branch`; the repository
default does not authorize or select the publication destination. The exact description is `v1 base=<40hex> approval=<64hex>`. Complete bounded
pagination and valid identities are required; malformed or uncertain observations fail closed.
The workflow token has read-only contents, status and pull-request permissions.

The owner-authorized publication process creates that status only for actually approved accepted
source and rechecks revocation immediately before merging. Approval does not replace functional
CI, security scans or independent acceptance. The workflow writes no status, merges nothing and
contains no bootstrap bypass. Replacing an older unconditional guard is separate owner maintenance
with exact protection restoration, outside the workflow. These policy changes do not require a
Home Assistant restart.

## Missing-context diagnosis

Only a complete, valid status history with no matching approval context emits one compact line
prefixed `OWNER_POLICY_GUARD_DIAGNOSIS `. Its closed JSON fields are `schema`, `reason`, `repository`,
`pr_number`, `head_sha` and `base_sha`. Schema is `owner-policy-guard-diagnosis-v1`; reason is
`MISSING_OWNER_APPROVAL`; PR number is a positive integer and both commits are exact lowercase
40-hex values. Repository and number bounds keep the line below 1 KiB. The marker is emitted only
after all non-approval checks pass, and the guard still exits with failure. Existing foreign,
revoked, pending, wrongly bound or malformed approval contexts never emit it. Neither do API,
identity, suppression or incomplete-pagination failures.

The stable guard step is named `Reject pull requests that weaken security policy`, with workflow
ID `owner_policy_guard`. Its run command invokes the default-branch Python file directly. GitHub
provides whole-job logs and numbered step metadata, not authenticated separate step stdout.
Consumers must authenticate the complete fixed workflow/job/run/attempt, event, head/base and guard
step before interpreting a single anchored marker from that step. Raw command echoes, untrusted
content, duplicate or unknown-field markers, truncated logs and ambiguous attribution are
ineligible; a substring is never sufficient proof. The official same-job rerun also reruns dependent
jobs; this fixed workflow declares none. Common publication orchestration owns any bounded rerun
against a genuinely new exact approval and retains the failed attempt. Historical successful job
logs do not establish a missing-approval diagnosis.

## Local verification

Run the pure fake-API/Git fixtures with
`python3 -I -S -B -m unittest discover -s tests_security -p 'test_owner_policy_guard.py' -v`.
`pyrightconfig.json` includes the guard and tests. Check both new Python files with the repository's
Ruff configuration, and run the existing offline `python3 tests/run.py` regression suite. Actual
GitHub transport, required CI and independent final-candidate review remain separate evidence.

Supported-base fixtures cover both approved branches, including the feature branch with a
`main` repository default. Unsupported, event/PR/live-ref mismatches and stale base commits
reject before status pagination or an absent-approval diagnosis. Exact owner approval,
revocation, suppression and same-repository identity checks apply equally to both branches.
