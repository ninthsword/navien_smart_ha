"""Pure trusted-base approval fixtures; no GitHub request or candidate execution."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / ".github/security/owner-policy-guard.py"
SPEC = importlib.util.spec_from_file_location("owner_policy_guard", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BASE = "a" * 40
HEAD = "b" * 40
APPROVAL = "c" * 64


def status(identity=1, **changes):
    return (
        dict(
            id=identity,
            context="owner-policy-approval",
            state="success",
            creator={"id": 11},
            created_at="2026-09-13T01:00:00Z",
            description=f"v1 base={BASE} approval={APPROVAL}",
        )
        | changes
    )


def repository() -> dict[str, Any]:
    return dict(
        id=22,
        full_name="owner/project",
        owner={"id": 11, "type": "User"},
        default_branch="main",
    )


def pull() -> dict[str, Any]:
    return dict(
        number=7,
        state="open",
        base={"sha": BASE, "ref": "main", "repo": repository()},
        head={"sha": HEAD, "ref": "feature", "repo": repository()},
    )


class OwnerApprovalTests(unittest.TestCase):
    def test_exact_owner_success_and_case_insensitive_context(self):
        self.assertEqual(
            MODULE.owner_approval([status(context="OWNER-Policy-Approval")], 11, BASE),
            APPROVAL,
        )

    def test_newest_matching_status_across_all_creators_revokes_older_success(self):
        for row in (
            status(2, creator={"id": 12}),
            status(2, state="pending"),
            status(2, state="failure"),
            status(2, state="error"),
            status(2, context="OWNER-POLICY-APPROVAL", creator={"id": 12}),
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                MODULE.owner_approval([row, status()], 11, BASE)
        newer = status(1, created_at="2026-09-13T01:01:00Z", state="failure")
        with self.assertRaises(ValueError):
            MODULE.owner_approval([status(2), newer], 11, BASE)

    def test_exact_description_owner_and_scalar_validation(self):
        for description in (
            None,
            "",
            f"v1 base={HEAD} approval={APPROVAL}",
            f"v1 base={BASE} approval={APPROVAL} extra",
            f"v1 base={BASE} approval=abc",
        ):
            with self.subTest(description=description), self.assertRaises(ValueError):
                MODULE.owner_approval([status(description=description)], 11, BASE)
        for change in (
            {"creator": {"id": True}},
            {"id": False},
            {"created_at": "yesterday"},
            {"created_at": "2026-02-30T00:00:00Z"},
            {"context": None},
            {"state": "unknown"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                MODULE.owner_approval([status() | change], 11, BASE)
        with self.assertRaises(ValueError):
            MODULE.owner_approval([], 11, BASE)

    def test_head_base_branch_repository_and_owner_binding(self):
        branch = {"name": "main", "commit": {"sha": BASE}}
        self.assertEqual(
            MODULE.validate_pull(
                pull(), repository(), branch, "owner/project", 7, HEAD, BASE, "main"
            ),
            11,
        )
        for side, key, value in [
            ("head", "sha", BASE),
            ("base", "sha", HEAD),
            ("base", "ref", "other"),
        ]:
            changed = pull()
            changed[side][key] = value
            with self.assertRaises(ValueError):
                MODULE.validate_pull(
                    changed,
                    repository(),
                    branch,
                    "owner/project",
                    7,
                    HEAD,
                    BASE,
                    "main",
                )
        for key, value in [
            ("id", 33),
            ("full_name", "fork/project"),
            ("owner", {"id": 12, "type": "User"}),
            ("owner", {"id": 11, "type": "Organization"}),
        ]:
            changed = pull()
            changed["head"]["repo"][key] = value
            with self.assertRaises(ValueError):
                MODULE.validate_pull(
                    changed,
                    repository(),
                    branch,
                    "owner/project",
                    7,
                    HEAD,
                    BASE,
                    "main",
                )
        for changed in (
            {"name": "main", "commit": {"sha": HEAD}},
            {"name": "other", "commit": {"sha": BASE}},
        ):
            with self.assertRaises(ValueError):
                MODULE.validate_pull(
                    pull(),
                    repository(),
                    changed,
                    "owner/project",
                    7,
                    HEAD,
                    BASE,
                    "main",
                )

    def test_complete_pagination_duplicates_bound_and_api_errors(self):
        first = [status(index) for index in range(1, 101)]
        pages = []

        def fetch(page):
            pages.append(page)
            return first if page == 1 else [status(101, state="failure")]

        rows = MODULE.collect_statuses(fetch)
        self.assertEqual(pages, [1, 2])
        with self.assertRaises(ValueError):
            MODULE.owner_approval(rows, 11, BASE)
        for invalid_fetch in (
            lambda page: first,
            lambda page: {"message": "denied"},
            lambda page: [status((page - 1) * 100 + index) for index in range(1, 101)],
        ):
            with self.assertRaises(ValueError):
                MODULE.collect_statuses(invalid_fetch)

    def test_approval_never_allows_inline_suppression(self):
        self.assertTrue(
            MODULE.policy_changes([".github/security/owner-policy-guard.py"], "+safe")
        )
        self.assertFalse(MODULE.policy_changes(["scripts/deploy.sh"], "+safe"))
        for marker in MODULE.MARKERS:
            with self.assertRaises(ValueError):
                MODULE.policy_changes(["ordinary.py"], "+code # " + marker)
        self.assertFalse(
            MODULE.policy_changes(["ordinary.py"], "-code # " + MODULE.MARKERS[1])
        )
        with self.assertRaises(ValueError):
            MODULE.policy_changes(
                ["ordinary.py"],
                "diff --git a/x b/x\n+++ b/x\n@@ -0,0 +1 @@\n+++ code # "
                + MODULE.MARKERS[1],
            )
        self.assertTrue(
            MODULE.policy_changes(
                [".github/security/guard.py"],
                "diff --git a/x b/x\n+++ b/" + MODULE.MARKERS[1] + ".py",
            )
        )

    def test_trusted_main_observes_exact_head_and_never_executes_candidate(self):
        env = {
            "BASE_SHA": BASE,
            "HEAD_SHA": HEAD,
            "GITHUB_REPOSITORY": "owner/project",
            "PR_NUMBER": "7",
            "EXPECTED_BASE_BRANCH": "main",
            "GITHUB_TOKEN": "fixture-only",
        }
        calls = []

        def observe(*args):
            calls.append(args)
            if args == ("rev-parse", "--is-shallow-repository"):
                return "false\n"
            if args[0] == "rev-parse":
                return HEAD + "\n"
            if args[0] == "rev-list":
                return HEAD + " " + BASE + "\n"
            if "--name-only" in args:
                return ".github/workflows/with\nnewline.yml\0"
            if "--patch" in args:
                return "diff --git a/x b/x\n@@ -0,0 +1 @@\n+safe\n"
            return ""

        responses = [
            repository(),
            pull(),
            {"name": "main", "commit": {"sha": BASE}},
            [status()],
        ]
        with (
            mock.patch.dict(MODULE.os.environ, env, clear=True),
            mock.patch.object(MODULE, "git", side_effect=observe),
            mock.patch.object(MODULE, "api", side_effect=responses) as api,
        ):
            self.assertEqual(MODULE.main(), 0)
            self.assertEqual(api.call_count, 4)
        self.assertIn(("fetch", "--no-tags", "origin", "refs/pull/7/head"), calls)
        self.assertTrue(any("-z" in args for args in calls))
        self.assertTrue(
            any("--no-ext-diff" in args and "--no-textconv" in args for args in calls)
        )
        self.assertTrue(
            all(
                args[0] in ("cat-file", "fetch", "rev-parse", "rev-list", "diff")
                for args in calls
            )
        )
        with (
            mock.patch.dict(MODULE.os.environ, env, clear=True),
            mock.patch.object(MODULE, "git", return_value="wrong"),
            mock.patch.object(MODULE, "api") as api,
        ):
            self.assertEqual(MODULE.main(), 1)
            api.assert_not_called()

    def test_merge_resolution_changes_are_guarded_even_without_ordinary_history(self):
        env = {
            "BASE_SHA": BASE,
            "HEAD_SHA": HEAD,
            "GITHUB_REPOSITORY": "owner/project",
            "PR_NUMBER": "7",
            "EXPECTED_BASE_BRANCH": "main",
            "GITHUB_TOKEN": "fixture-only",
        }

        def observe(*args):
            if args == ("rev-parse", "--is-shallow-repository"):
                return "false\n"
            if args[0] == "rev-parse":
                return HEAD + "\n"
            if args[0] == "rev-list":
                return HEAD + " " + BASE + "\n"
            if args[0] == "diff" and "--name-only" in args:
                return ".github/workflows/merge.yml\0"
            return ""

        responses = [
            repository(),
            pull(),
            {"name": "main", "commit": {"sha": BASE}},
            [status(state="failure")],
        ]
        with (
            mock.patch.dict(MODULE.os.environ, env, clear=True),
            mock.patch.object(MODULE, "git", side_effect=observe),
            mock.patch.object(MODULE, "api", side_effect=responses) as api,
        ):
            self.assertEqual(MODULE.main(), 1)
            self.assertEqual(api.call_count, 4)


class MissingApprovalDiagnosisTests(unittest.TestCase):
    def run_guard(
        self,
        pages=None,
        *,
        changed_pull=None,
        changed_branch=None,
        env_change=None,
        git_failure=None,
        patch="+safe",
    ):
        env = {
            "BASE_SHA": BASE,
            "HEAD_SHA": HEAD,
            "GITHUB_REPOSITORY": "owner/project",
            "PR_NUMBER": "7",
            "EXPECTED_BASE_BRANCH": "main",
            "GITHUB_TOKEN": "fixture-only",
        }
        env.update(env_change or {})

        def observe(*args):
            if git_failure:
                raise ValueError("Synthetic observation failure")
            if args == ("rev-parse", "--is-shallow-repository"):
                return "false\n"
            if args[0] == "rev-parse":
                return HEAD + "\n"
            if args[0] == "rev-list":
                return HEAD + " " + BASE + "\n"
            if "--name-only" in args:
                return ".github/security/guard.py\0"
            if "--patch" in args:
                return patch
            return ""

        responses = [
            repository(),
            changed_pull or pull(),
            changed_branch or {"name": "main", "commit": {"sha": BASE}},
        ]
        responses += [[]] if pages is None else pages
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(MODULE.os.environ, env, clear=True),
            mock.patch.object(MODULE, "git", side_effect=observe),
            mock.patch.object(MODULE, "api", side_effect=responses) as api,
            mock.patch.object(MODULE.sys, "stdout", stdout),
            mock.patch.object(MODULE.sys, "stderr", stderr),
        ):
            result = MODULE.main()
        return result, stdout.getvalue(), stderr.getvalue(), api.call_count

    def test_both_supported_bases_accept_exact_owner_approval(self):
        for name in ("main", "feature/boiler-nr67d"):
            changed = pull()
            changed["base"]["ref"] = name
            with self.subTest(branch=name):
                result, output, error, calls = self.run_guard(
                    [[status()]],
                    changed_pull=changed,
                    changed_branch={"name": name, "commit": {"sha": BASE}},
                    env_change={"EXPECTED_BASE_BRANCH": name},
                )
                self.assertEqual(result, 0)
                self.assertEqual(error, "")
                self.assertNotIn("OWNER_POLICY_GUARD_DIAGNOSIS", output)
                self.assertEqual(calls, 4)

    def test_supported_base_absence_keeps_exact_diagnosis(self):
        for name in ("main", "feature/boiler-nr67d"):
            changed = pull()
            changed["base"]["ref"] = name
            with self.subTest(branch=name):
                result, output, _, calls = self.run_guard(
                    changed_pull=changed,
                    changed_branch={"name": name, "commit": {"sha": BASE}},
                    env_change={"EXPECTED_BASE_BRANCH": name},
                )
                self.assertEqual(result, 1)
                self.assertEqual(calls, 4)
                self.assertEqual(len(output.splitlines()), 1)
                value = json.loads(output.removeprefix("OWNER_POLICY_GUARD_DIAGNOSIS "))
                self.assertEqual(value["reason"], "MISSING_OWNER_APPROVAL")
                self.assertEqual((value["head_sha"], value["base_sha"]), (HEAD, BASE))

    def test_unsupported_mismatched_and_stale_bases_fail_before_statuses(self):
        selected = "feature/boiler-nr67d"
        cases = [
            ("other", "other", "other", BASE),
            ("feature/boiler-nr67d/extra", selected, selected, BASE),
            (selected, "main", selected, BASE),
            (selected, selected, "main", BASE),
            (selected, selected, selected, HEAD),
            ("main", selected, "main", BASE),
        ]
        for expected, pull_ref, live_ref, live_sha in cases:
            changed = pull()
            changed["base"]["ref"] = pull_ref
            with self.subTest(expected=expected, pull_ref=pull_ref, live_ref=live_ref):
                result, output, _, calls = self.run_guard(
                    changed_pull=changed,
                    changed_branch={"name": live_ref, "commit": {"sha": live_sha}},
                    env_change={"EXPECTED_BASE_BRANCH": expected},
                )
                self.assertEqual(result, 1)
                self.assertEqual(output, "")
                self.assertLess(calls, 4)
        with self.assertRaises(ValueError):
            MODULE.validate_pull(
                pull(), repository(), {"name": "other", "commit": {"sha": BASE}},
                "owner/project", 7, HEAD, BASE, "other",
            )

    def test_complete_absence_emits_one_bounded_exact_bound_diagnosis_and_still_fails(
        self,
    ):
        for pages in (
            None,
            [[status(context="unrelated")]],
            [[status(index, context="unrelated") for index in range(1, 101)], []],
        ):
            result, output, error, calls = self.run_guard(pages)
            self.assertEqual(result, 1)
            self.assertIn("rejected", error)
            self.assertLess(len(output.encode()), 1024)
            self.assertEqual(len(output.splitlines()), 1)
            prefix = "OWNER_POLICY_GUARD_DIAGNOSIS "
            self.assertTrue(output.startswith(prefix))
            self.assertEqual(
                json.loads(output[len(prefix) :]),
                {
                    "schema": "owner-policy-guard-diagnosis-v1",
                    "reason": "MISSING_OWNER_APPROVAL",
                    "repository": "owner/project",
                    "pr_number": 7,
                    "base_sha": BASE,
                    "head_sha": HEAD,
                },
            )
            self.assertEqual(calls, 5 if pages is not None and len(pages) == 2 else 4)

    def test_existing_invalid_or_revoked_context_is_never_absence(self):
        for rows in (
            [status(2, creator={"id": 12}), status()],
            [status(state="pending")],
            [status(state="failure")],
            [status(state="error")],
            [status(description=f"v1 base={HEAD} approval={APPROVAL}")],
            [status(description=None)],
            [status(context="OWNER-POLICY-APPROVAL", creator={"id": 12})],
            [status(context="other", created_at="invalid")],
            [status(context=None)],
            [status(context="other", creator={"id": True})],
            [status(), status()],
        ):
            with self.subTest(rows=rows):
                result, output, _, _ = self.run_guard([rows])
                self.assertEqual(result, 1)
                self.assertEqual(output, "")
        result, output, _, _ = self.run_guard([[status()]])
        self.assertEqual(result, 0)
        self.assertNotIn("OWNER_POLICY_GUARD_DIAGNOSIS", output)

    def test_failed_or_incomplete_status_observation_never_emits_diagnosis(self):
        incomplete = [
            [status(page * 100 + index, context="other") for index in range(1, 101)]
            for page in range(MODULE.MAX_PAGES)
        ]
        for pages in (
            [[{"id": 1}]],
            [{"message": "denied"}],
            [OSError("Synthetic API failure")],
            incomplete,
        ):
            with self.subTest(pages_count=len(pages)):
                result, output, _, _ = self.run_guard(pages)
                self.assertEqual(result, 1)
                self.assertEqual(output, "")

    def test_nonapproval_failures_cannot_emit_marker_or_reach_statuses(self):
        changed = pull()
        changed["head"]["repo"]["id"] = 33
        cases: tuple[dict[str, Any], ...] = (
            {"git_failure": True},
            {"patch": "+code # " + MODULE.MARKERS[0]},
            {"env_change": {"BASE_SHA": "bad"}},
            {"env_change": {"GITHUB_TOKEN": ""}},
            {"env_change": {"GITHUB_REPOSITORY": "x" * 257 + "/repo"}},
            {"env_change": {"PR_NUMBER": str(2**63)}},
            {"changed_pull": changed},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                result, output, _, calls = self.run_guard(**kwargs)
                self.assertEqual(result, 1)
                self.assertEqual(output, "")
                self.assertLess(calls, 4)

    def test_reverted_suppression_still_rejects_before_any_approval_observation(self):
        marker = MODULE.MARKERS[0]
        patch = (
            "diff --git a/x b/x\n@@ -0,0 +1 @@\n+code # "
            + marker
            + "\ndiff --git a/x b/x\n@@ -1 +0,0 @@\n-code # "
            + marker
            + "\n"
        )
        result, output, _, calls = self.run_guard([[status()]], patch=patch)
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertEqual(calls, 0)

    def test_all_guard_and_scanner_policy_paths_require_approval(self):
        for path in (
            ".github/security/owner-policy-guard.py",
            ".github/security/README.md",
            ".github/workflows/security-policy.yml",
            ".gitleaksignore",
            "nested/.gitleaks.toml",
            "nested/.semgrepignore",
        ):
            with self.subTest(path=path):
                self.assertTrue(MODULE.policy_changes([path], "+safe"))
        self.assertFalse(
            MODULE.policy_changes(
                ["custom_components/navien_smarthome/manifest.json"], "+safe"
            )
        )

    def test_workflow_has_one_fixed_trusted_guard_step_and_no_inline_marker(self):
        workflow = (SCRIPT.parents[1] / "workflows/security-policy.yml").read_text()
        self.assertEqual(workflow.count("id: owner_policy_guard"), 1)
        self.assertEqual(
            workflow.count("name: Reject pull requests that weaken security policy"), 1
        )
        self.assertEqual(
            workflow.count(
                "run: python3 -I -S -B .github/security/owner-policy-guard.py"
            ),
            1,
        )
        self.assertIn(
            "EXPECTED_BASE_BRANCH: ${{ github.event.pull_request.base.ref }}", workflow
        )
        self.assertNotIn("github.event.repository.default_branch", workflow)
        self.assertNotIn("OWNER_POLICY_GUARD_DIAGNOSIS", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertNotIn("needs:", workflow)


class GitHistoryTests(unittest.TestCase):
    """Real local Git DAGs exercise history rather than mocked diff text."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
                                GIT_CONFIG_GLOBAL='/dev/null', GIT_AUTHOR_NAME='Fixture',
                                GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_NAME='Fixture',
                                GIT_COMMITTER_EMAIL='fixture@example.invalid')
        self.command('init', '--quiet')
        self.files: set[str] = set()
        self.safe = {'ordinary.py': 'value = 1\n', '.github/workflows/check.yml': 'safe\n'}
        self.base = self.make_commit(self.safe)
        self.calls: list[tuple[str, ...]] = []

    def command(self, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(['git', '-C', str(self.root), *args], input=input_text,
                                capture_output=True, text=True, env=self.environment, check=True)
        return result.stdout

    def make_commit(self, files: dict[str, str], *parents: str) -> str:
        for name in self.files:
            (self.root / name).unlink()
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.files = set(files)
        self.command('add', '-A')
        tree = self.command('write-tree').strip()
        arguments = ['commit-tree', tree]
        for parent in parents:
            arguments += ['-p', parent]
        return self.command(*arguments, input_text='Offline history fixture\n').strip()

    def observe(self, *args: str) -> str:
        self.calls.append(args)
        return self.command(*args)

    def run_guard(self, head: str, *, protected: bool = False, reject: bool = False):
        env = {'BASE_SHA': self.base, 'HEAD_SHA': head, 'GITHUB_REPOSITORY': 'owner/project',
               'PR_NUMBER': '7', 'EXPECTED_BASE_BRANCH': 'main', 'GITHUB_TOKEN': 'fixture-only'}
        def observe(*args: str) -> str:
            if args[0] == 'fetch':
                return ''  # Only transport is stubbed; the full commit DAG is real.
            if args == ('rev-parse', 'FETCH_HEAD'):
                return head + '\n'
            return self.observe(*args)
        request = pull()
        request['base']['sha'], request['head']['sha'] = self.base, head
        rows = [status(description=f'v1 base={self.base} approval={APPROVAL}')]
        responses = [repository(), request, {'name': 'main', 'commit': {'sha': self.base}}, rows]
        output = io.StringIO()
        with mock.patch.dict(MODULE.os.environ, env, clear=True), \
             mock.patch.object(MODULE, 'git', side_effect=observe), \
             mock.patch.object(MODULE, 'api', side_effect=responses) as api, \
             mock.patch.object(MODULE.sys, 'stdout', output), \
             mock.patch.object(MODULE.sys, 'stderr', io.StringIO()):
            result = MODULE.main()
        self.assertEqual(result, 1 if reject else 0)
        self.assertEqual(api.call_count, 0 if reject or not protected else 4)
        self.assertNotIn('OWNER_POLICY_GUARD_DIAGNOSIS', output.getvalue())

    def test_ordinary_and_reverted_suppression_rejected_before_approval(self):
        for marker in MODULE.MARKERS:
            with self.subTest(marker=marker):
                bad = self.make_commit(self.safe | {'ordinary.py': 'value = 1 # ' + marker + '\n'}, self.base)
                self.run_guard(bad, reject=True)
                tip = self.make_commit(self.safe, bad)
                self.run_guard(tip, reject=True)

    def test_merge_only_suppression_later_reverted_is_rejected(self):
        left_files = self.safe | {'left.txt': 'left\n'}
        right_files = self.safe | {'right.txt': 'right\n'}
        left = self.make_commit(left_files, self.base)
        right = self.make_commit(right_files, self.base)
        combined = left_files | right_files
        merge = self.make_commit(combined | {'ordinary.py': 'value = 1 # ' + MODULE.MARKERS[1] + '\n'}, left, right)
        tip = self.make_commit(combined, merge)
        self.run_guard(tip, reject=True)

    def test_safe_merge_and_nonfirst_parent_comparison(self):
        left = self.make_commit(self.safe | {'left.txt': 'left\n'}, self.base)
        right = self.make_commit(self.safe | {'right.txt': 'right\n'}, self.base)
        merge = self.make_commit(self.safe | {'left.txt': 'left\n'}, left, right)
        self.run_guard(merge)
        self.assertIn(('diff', '--patch', '--unified=0', '--no-ext-diff', '--no-textconv',
                       '--no-renames', right, merge, '--'), self.calls)
        self.assertEqual(self.command('diff', '--name-only', left, merge), '')
        self.assertNotEqual(self.command('diff', '--name-only', right, merge), '')

    def test_merge_only_protected_change_later_reverted_requires_approval(self):
        left = self.make_commit(self.safe | {'left.txt': 'left\n'}, self.base)
        right = self.make_commit(self.safe | {'right.txt': 'right\n'}, self.base)
        combined = self.safe | {'left.txt': 'left\n', 'right.txt': 'right\n'}
        merge = self.make_commit(combined | {'.github/workflows/check.yml': 'changed\n'}, left, right)
        tip = self.make_commit(combined, merge)
        self.run_guard(tip, protected=True)

    def test_rename_out_and_back_and_newline_paths_require_approval(self):
        self.command('config', 'diff.renames', 'true')
        renamed = {'ordinary.py': self.safe['ordinary.py'], 'moved.yml': 'safe\n'}
        move = self.make_commit(renamed, self.base)
        tip = self.make_commit(self.safe, move)
        self.run_guard(tip, protected=True)
        newline = self.make_commit(self.safe | {'.github/workflows/a\nb.yml': 'safe\n'}, self.base)
        self.run_guard(newline, protected=True)
        self.assertTrue(all('--no-renames' in args for args in self.calls
                            if args[0] in ('diff', 'diff-tree')))

    def test_imported_parentless_safe_and_unsafe_roots_are_inspected(self):
        # Even if Git's presentation config suppresses root patches, explicit --root wins.
        self.command('config', 'log.showRoot', 'false')
        for bad in (False, True):
            with self.subTest(bad=bad):
                content = 'value = 1' + (' # ' + MODULE.MARKERS[2] if bad else '') + '\n'
                root = self.make_commit({'imported.py': content})
                merge = self.make_commit(self.safe, self.base, root)
                self.run_guard(merge, protected=True, reject=bad)
                self.assertIn(('diff-tree', '--root', '--no-commit-id', '-r', '--name-only',
                               '-z', '--no-renames', root, '--'), self.calls)

    def test_incomplete_shallow_history_is_rejected_before_api(self):
        tip = self.make_commit(self.safe | {'safe.txt': 'safe\n'}, self.base)
        (self.root / '.git/shallow').write_text(tip + '\n')
        self.run_guard(tip, reject=True)

    def test_history_limits_invalid_parents_and_incomplete_records_fail_closed(self):
        tip = self.make_commit(self.safe | {'safe.txt': 'safe\n'}, self.base)
        raw = self.command('rev-list', '--parents', self.base + '..' + tip)
        malformed = (raw.rstrip('\n'), raw.replace('\n', '\r\n'), raw + raw, tip + ' BAD\n', tip + '  ' + self.base + '\n',
                     tip + ' ' + tip + '\n', '', self.base + '\n',
                     tip + ' ' + self.base + ' ' + self.base + '\n')
        for traversal in malformed:
            with self.subTest(traversal=traversal):
                def observe(*args: str, value: str = traversal) -> str:
                    return value if args[0] == 'rev-list' else self.observe(*args)
                with mock.patch.object(MODULE, 'git', side_effect=observe), self.assertRaises(ValueError):
                    MODULE.policy_history(self.base, tip)
        second = self.make_commit(self.safe, tip)
        with mock.patch.object(MODULE, 'git', side_effect=self.observe), \
             mock.patch.object(MODULE, 'MAX_COMMITS', 1), self.assertRaises(ValueError):
            MODULE.policy_history(self.base, second)
        with mock.patch.object(MODULE, 'git', side_effect=self.observe), \
             mock.patch.object(MODULE, 'MAX_BYTES', 1), self.assertRaises(ValueError):
            MODULE.policy_history(self.base, tip)

    def test_failed_git_invalid_utf8_and_oversized_output_reject(self):
        for code, raw in ((1, b''), (0, b'\xff'), (0, b'x' * (MODULE.MAX_BYTES + 1))):
            with self.subTest(code=code, size=len(raw)), \
                 mock.patch.object(MODULE.subprocess, 'run', return_value=subprocess.CompletedProcess(['git'], code, raw, b'')), \
                 self.assertRaises(ValueError):
                MODULE.git('rev-list', '--parents', BASE + '..' + HEAD)
        tip = self.make_commit(self.safe | {'safe.txt': 'safe\n'}, self.base)
        for names in ('unterminated', '\0', 'safe\0\0'):
            def observe(*args: str, value: str = names) -> str:
                return value if '--name-only' in args else self.observe(*args)
            with mock.patch.object(MODULE, 'git', side_effect=observe), self.assertRaises(ValueError):
                MODULE.policy_history(self.base, tip)


if __name__ == "__main__":
    unittest.main()
