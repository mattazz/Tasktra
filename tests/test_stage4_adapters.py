from pathlib import Path
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from tasktra.provider_adapters import (
    BoundedArgvRunner, CommandResult, GitAdapter, GitHubCliAdapter, JiraConnectorAdapter,
    MAX_COMMAND_OUTPUT_BYTES, git_scope_fingerprint,
)
from tasktra.providers import OperationDescriptor, ProviderError, ResourceScope


def scope(provider, *, resource="12", ref=None):
    return ResourceScope(provider, f"{provider}.example", "acme/widgets", "issue", resource, ref)


def jira_scope(*, resource="ABC-12"):
    return ResourceScope("jira", "jira.example", "ABC", "issue", resource)


def result(state="succeeded", summary="ok", items=None):
    return {"kind": "tasktra.provider-result", "version": 1, "state": state, "summary": summary, "items": items or []}


class Stage4AdapterTests(unittest.TestCase):
    def test_runner_is_argv_only_timeout_bounded_and_never_shell(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            return CommandResult(0, b"x" * (MAX_COMMAND_OUTPUT_BYTES + 10), b"")

        with TemporaryDirectory() as directory:
            output = BoundedArgvRunner(invoke).run(["git", "status"], cwd=Path(directory), env={})
        self.assertFalse(calls[0]["shell"])
        self.assertEqual(len(output.stdout), MAX_COMMAND_OUTPUT_BYTES)
        self.assertTrue(output.output_limited)

        timeout = BoundedArgvRunner(lambda **_: CommandResult(124, b"partial", b"late", True)).run(
            ["git", "status"], cwd=Path.cwd(), env={}
        )
        self.assertTrue(timeout.timed_out)
        self.assertEqual(timeout.stdout, b"partial")

        uncertain = BoundedArgvRunner(
            lambda **_: CommandResult(127, input_uncertain=True)
        ).run(["git", "status"], cwd=Path.cwd(), env={}, stdin=b"payload")
        self.assertTrue(uncertain.input_uncertain)
        self.assertTrue(uncertain.dispatched)

    def test_live_runner_timeout_covers_a_child_that_never_reads_stdin(self):
        started = time.monotonic()
        output = BoundedArgvRunner(timeout=1).run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=Path.cwd(), env={"PATH": ""}, stdin=b"x" * (1024 * 1024),
        )
        self.assertTrue(output.timed_out)
        self.assertLess(time.monotonic() - started, 6)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object descendant cleanup")
    def test_windows_timeout_kills_descendant_when_taskkill_is_unavailable(self):
        with TemporaryDirectory() as directory:
            sentinel = Path(directory) / "descendant-survived"
            child = (
                "import time; from pathlib import Path; time.sleep(2); "
                f"Path({str(sentinel)!r}).write_text('survived', encoding='utf-8')"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "time.sleep(10)"
            )
            with patch("tasktra.provider_adapters.subprocess.run", side_effect=OSError("taskkill unavailable")):
                output = BoundedArgvRunner(timeout=1).run(
                    [sys.executable, "-c", parent], cwd=Path(directory), env={"PATH": ""},
                )
            self.assertTrue(output.timed_out)
            time.sleep(2.5)
            self.assertFalse(sentinel.exists(), "timed-out provider command left a descendant running")

    @unittest.skipUnless(os.name == "nt", "Windows suspended launch containment")
    def test_windows_job_assignment_failure_never_dispatches_provider_process(self):
        with TemporaryDirectory() as directory:
            sentinel = Path(directory) / "uncontained-descendant"
            child = (
                "import time; from pathlib import Path; time.sleep(2); "
                f"Path({str(sentinel)!r}).write_text('survived', encoding='utf-8')"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "time.sleep(10)"
            )
            with (
                patch("tasktra.provider_adapters._WindowsJob.assign", side_effect=OSError("job denied")),
                patch("tasktra.provider_adapters.subprocess.run", side_effect=OSError("taskkill unavailable")),
            ):
                output = BoundedArgvRunner(timeout=1).run(
                    [sys.executable, "-c", parent], cwd=Path(directory), env={"PATH": ""},
                )
            self.assertFalse(output.dispatched)
            time.sleep(2.5)
            self.assertFalse(sentinel.exists(), "uncontained provider process executed after job assignment failed")

    def test_git_validates_injection_refuses_unrelated_stage_and_push_is_exact(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            calls = []

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "rev-parse" in argv:
                    return CommandResult(0, str(repo).encode())
                if "get-url" in argv:
                    return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                if "--get-url" in argv:
                    return CommandResult(0, (argv[-1] + "\n").encode())
                if "config" in argv and "--get-regexp" in argv:
                    return CommandResult(1)
                if "--cached" in argv:
                    return CommandResult(0, b"safe.txt\0other.txt\0")
                if "ls-remote" in argv:
                    return CommandResult(0, b"0123456789abcdef0123456789abcdef01234567\trefs/heads/main\n")
                return CommandResult(0, b"ok")

            adapter = GitAdapter(repo, BoundedArgvRunner(invoke))
            status = OperationDescriptor("git", "read-only", "git-status")
            commit = OperationDescriptor("git", "repository-history", "git-commit")
            push = OperationDescriptor("git", "repository-history", "git-push")
            fingerprint = git_scope_fingerprint(repo, "github.com", "acme/widgets")
            git_scope = ResourceScope("git", "github.com", "acme/widgets", "repository", fingerprint)
            commit_scope = ResourceScope("git", "github.com", "acme/widgets", "repository",
                                         fingerprint, "refs/heads/main")
            self.assertEqual(adapter.read(status, git_scope, {}).state, "succeeded")
            with self.assertRaisesRegex(ProviderError, "unrelated"):
                adapter.execute(commit, commit_scope, {
                    "paths": ["safe.txt"], "message": "safe",
                    "expected_parent_oid": "0123456789abcdef0123456789abcdef01234567",
                }, "commit-1")
            with self.assertRaisesRegex(ProviderError, "contained"):
                adapter.read(OperationDescriptor("git", "read-only", "git-diff"), git_scope, {"path": "../oops"})
            old_oid, new_oid = "0123456789abcdef0123456789abcdef01234567", "fedcba9876543210fedcba9876543210fedcba98"
            request = {"remote": "origin", "ref": "refs/heads/main", "expected_old_oid": old_oid, "new_oid": new_oid}
            push_scope = ResourceScope("git", "github.com", "acme/widgets", "repository",
                                       fingerprint, "refs/heads/main")
            self.assertEqual(adapter.execute(push, push_scope, request, "push-1").state, "succeeded")
            pushes = [call for call in calls if "push" in call]
            self.assertEqual(len(pushes), 1)
            self.assertIn("--no-verify", pushes[0])
            self.assertLess(pushes[0].index("--no-verify"), pushes[0].index("https://github.com/acme/widgets.git"))
            for option in ("--no-follow-tags", "--recurse-submodules=no", "--no-push-option", "--no-signed",
                           "--no-force-if-includes"):
                self.assertIn(option, pushes[0])
            self.assertEqual(pushes[0][-3:], [f"--force-with-lease=refs/heads/main:{old_oid}", "https://github.com/acme/widgets.git", f"{new_oid}:refs/heads/main"])
            with self.assertRaisesRegex(ProviderError, "full 40"):
                adapter.execute(push, push_scope, {"remote": "origin", "ref": "refs/heads/main", "expected_old_oid": old_oid}, "push-2")

    def test_git_push_conflict_and_reconciliation_never_blind_retry(self):
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "rev-parse" in argv:
                    return CommandResult(0, str(repo).encode())
                if "get-url" in argv:
                    return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                if "--get-url" in argv:
                    return CommandResult(0, (argv[-1] + "\n").encode())
                if "config" in argv and "--get-regexp" in argv:
                    return CommandResult(1)
                if "ls-remote" in argv:
                    return CommandResult(0, b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/heads/main\n")
                return CommandResult(0, b"unexpected")

            adapter = GitAdapter(repo, BoundedArgvRunner(invoke))
            descriptor = OperationDescriptor("git", "repository-history", "git-push")
            old_oid, new_oid = "0123456789abcdef0123456789abcdef01234567", "fedcba9876543210fedcba9876543210fedcba98"
            request = {"remote": "origin", "ref": "refs/heads/main", "expected_old_oid": old_oid, "new_oid": new_oid}
            git_scope = ResourceScope("git", "github.com", "acme/widgets", "repository",
                                      git_scope_fingerprint(repo, "github.com", "acme/widgets"), "refs/heads/main")
            outcome = adapter.execute(descriptor, git_scope, request, "push-1")
            self.assertEqual(outcome.state, "failed")
            self.assertIn("conflicts", outcome.summary)
            self.assertFalse(any("push" in call for call in calls))

            def remote(oid, *, timed_out=False):
                def invoke_remote(**kwargs):
                    argv = list(kwargs["argv"])
                    if "rev-parse" in argv:
                        return CommandResult(0, str(repo).encode())
                    if "get-url" in argv:
                        return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                    if "--get-url" in argv:
                        return CommandResult(0, (argv[-1] + "\n").encode())
                    if "config" in argv and "--get-regexp" in argv:
                        return CommandResult(1)
                    if "ls-remote" in argv:
                        return CommandResult(124 if timed_out else 0, f"{oid}\trefs/heads/main\n".encode(), timed_out=timed_out)
                    return CommandResult(0, str(repo).encode())
                return GitAdapter(repo, BoundedArgvRunner(invoke_remote))

            self.assertEqual(remote(new_oid).reconcile_push(git_scope, request).state, "reconciled")
            absent = remote(old_oid).reconcile_push(git_scope, request)
            self.assertEqual(absent.state, "absent")
            self.assertIn("absent", absent.summary)
            self.assertEqual(remote("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").reconcile_push(git_scope, request).state, "conflict")
            self.assertEqual(remote(old_oid, timed_out=True).reconcile_push(git_scope, request).state, "indeterminate")

    def test_git_push_requires_bound_repository_fingerprint_and_resolved_destination(self):
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "rev-parse" in argv:
                    return CommandResult(0, str(repo).encode())
                if "get-url" in argv:
                    return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                if "--get-url" in argv:
                    return CommandResult(0, (argv[-1] + "\n").encode())
                if "config" in argv and "--get-regexp" in argv:
                    return CommandResult(1)
                if "ls-remote" in argv:
                    return CommandResult(0, b"0123456789abcdef0123456789abcdef01234567\trefs/heads/main\n")
                return CommandResult(0, b"ok")

            adapter = GitAdapter(repo, BoundedArgvRunner(invoke))
            descriptor = OperationDescriptor("git", "repository-history", "git-push")
            old_oid, new_oid = "0123456789abcdef0123456789abcdef01234567", "fedcba9876543210fedcba9876543210fedcba98"
            request = {"remote": "origin", "ref": "refs/heads/main", "expected_old_oid": old_oid, "new_oid": new_oid}
            wrong_fingerprint = ResourceScope("git", "github.com", "acme/widgets", "repository", "0" * 64,
                                              "refs/heads/main")
            with self.assertRaisesRegex(ProviderError, "fingerprint"):
                adapter.execute(descriptor, wrong_fingerprint, request, "push-bad-fingerprint")
            self.assertFalse(any("push" in call for call in calls))

            changed_scope = ResourceScope("git", "github.com", "acme/widgets", "repository",
                                          git_scope_fingerprint(repo, "github.com", "acme/widgets"),
                                          "refs/heads/main")
            calls.clear()

            def changed_remote(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "rev-parse" in argv:
                    return CommandResult(0, str(repo).encode())
                if "get-url" in argv:
                    return CommandResult(0, b"https://github.com/other/repository.git\n")
                return CommandResult(0, b"ok")

            with self.assertRaisesRegex(ProviderError, "destination"):
                GitAdapter(repo, BoundedArgvRunner(changed_remote)).execute(descriptor, changed_scope, request, "push-changed-destination")
            self.assertFalse(any("push" in call for call in calls))

    def test_git_commit_uses_verified_tree_and_atomic_ref_compare_and_swap(self):
        old_oid = "0123456789abcdef0123456789abcdef01234567"
        tree_oid = "1111111111111111111111111111111111111111"
        new_oid = "fedcba9876543210fedcba9876543210fedcba98"
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []

            def adapter_for(*, branch="refs/heads/main", update_code=0):
                def invoke(**kwargs):
                    argv = list(kwargs["argv"]); calls.append(argv)
                    if "--show-toplevel" in argv:
                        return CommandResult(0, str(repo).encode())
                    if "--cached" in argv:
                        return CommandResult(0, b"safe.txt\0")
                    if "symbolic-ref" in argv:
                        return CommandResult(0, branch.encode())
                    if "HEAD^{commit}" in argv:
                        return CommandResult(0, old_oid.encode())
                    if "write-tree" in argv:
                        return CommandResult(0, tree_oid.encode())
                    if "diff-tree" in argv:
                        return CommandResult(0, b"safe.txt\0")
                    if "commit-tree" in argv:
                        return CommandResult(0, new_oid.encode())
                    if f"{new_oid}^1" in argv:
                        return CommandResult(0, old_oid.encode())
                    if "update-ref" in argv:
                        return CommandResult(update_code)
                    return CommandResult(1)
                return GitAdapter(repo, BoundedArgvRunner(invoke))

            commit_scope = ResourceScope(
                "git", "github.com", "acme/widgets", "repository",
                git_scope_fingerprint(repo, "github.com", "acme/widgets"), "refs/heads/main",
            )
            descriptor = OperationDescriptor("git", "repository-history", "git-commit")
            request = {"paths": ["safe.txt"], "message": "safe", "expected_parent_oid": old_oid}
            outcome = adapter_for().execute(descriptor, commit_scope, request, "commit-1")
            self.assertEqual(outcome.state, "succeeded")
            self.assertTrue(any(call[-4:] == ["update-ref", "refs/heads/main", new_oid, old_oid] for call in calls))
            self.assertFalse(any("commit" in call and "commit-tree" not in call for call in calls))

            calls.clear()
            with self.assertRaisesRegex(ProviderError, "branch differs"):
                adapter_for(branch="refs/heads/other").execute(descriptor, commit_scope, request, "commit-2")
            self.assertFalse(any("update-ref" in call for call in calls))

            calls.clear()
            outcome = adapter_for(update_code=1).execute(descriptor, commit_scope, request, "commit-3")
            self.assertEqual(outcome.state, "indeterminate")

    def test_git_initial_commit_requires_unborn_branch_and_atomic_zero_parent(self):
        tree_oid = "1" * 40
        new_oid = "f" * 40
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "--show-toplevel" in argv:
                    return CommandResult(0, str(repo).encode())
                if "--cached" in argv:
                    return CommandResult(0, b".tasktra/project.toml\0safe.txt\0")
                if "symbolic-ref" in argv:
                    return CommandResult(0, b"refs/heads/main")
                if "HEAD^{commit}" in argv:
                    return CommandResult(1)
                if "write-tree" in argv:
                    return CommandResult(0, tree_oid.encode())
                if "ls-tree" in argv:
                    return CommandResult(0, b".tasktra/project.toml\0safe.txt\0")
                if "commit-tree" in argv:
                    return CommandResult(0, new_oid.encode())
                if "rev-list" in argv:
                    return CommandResult(0, new_oid.encode())
                if "update-ref" in argv:
                    return CommandResult(0)
                return CommandResult(1)

            scope = ResourceScope(
                "git", "github.com", "acme/widgets", "repository",
                git_scope_fingerprint(repo, "github.com", "acme/widgets"), "refs/heads/main",
            )
            outcome = GitAdapter(repo, BoundedArgvRunner(invoke)).execute(
                OperationDescriptor("git", "repository-history", "git-commit"), scope,
                {"paths": [".tasktra/project.toml", "safe.txt"], "message": "initial", "expected_parent_oid": None}, "initial-commit",
            )
            self.assertEqual(outcome.state, "succeeded")
            self.assertEqual(outcome.items[0]["parent_oid"], None)
            self.assertTrue(any(call[-4:] == ["update-ref", "refs/heads/main", new_oid, "0" * 40] for call in calls))
            commit_tree = next(call for call in calls if "commit-tree" in call)
            self.assertNotIn("-p", commit_tree)

    def test_git_first_push_requires_absent_remote_ref(self):
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []
            new_oid = "f" * 40

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "--show-toplevel" in argv:
                    return CommandResult(0, str(repo).encode())
                if "remote" in argv and "get-url" in argv:
                    return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                if "config" in argv and "--get-regexp" in argv:
                    return CommandResult(1)
                if "ls-remote" in argv and "--get-url" in argv:
                    return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                if "ls-remote" in argv:
                    return CommandResult(0, b"")
                if "push" in argv:
                    return CommandResult(0, b"ok")
                return CommandResult(1)

            scope = ResourceScope(
                "git", "github.com", "acme/widgets", "repository",
                git_scope_fingerprint(repo, "github.com", "acme/widgets"), "refs/heads/main",
            )
            request = {"remote": "origin", "ref": "refs/heads/main", "expected_old_oid": "0" * 40, "new_oid": new_oid}
            outcome = GitAdapter(repo, BoundedArgvRunner(invoke)).execute(
                OperationDescriptor("git", "repository-history", "git-push"), scope, request, "initial-push",
            )
            self.assertEqual(outcome.state, "succeeded")
            push = next(call for call in calls if "push" in call)
            self.assertIn("--force-with-lease=refs/heads/main:", push)

    def test_git_scopes_bind_each_read_and_reject_unsafe_remote_urls(self):
        old_oid = "0123456789abcdef0123456789abcdef01234567"
        new_oid = "fedcba9876543210fedcba9876543210fedcba98"
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "--show-toplevel" in argv:
                    return CommandResult(0, str(repo).encode())
                return CommandResult(0, b"")

            adapter = GitAdapter(repo, BoundedArgvRunner(invoke))
            fingerprint = git_scope_fingerprint(repo, "github.com", "acme/widgets")
            base_scope = ResourceScope("git", "github.com", "acme/widgets", "repository", fingerprint)
            with self.assertRaisesRegex(ProviderError, "fingerprint"):
                adapter.read(OperationDescriptor("git", "read-only", "git-status"),
                             ResourceScope("git", "github.com", "acme/widgets", "repository", "0" * 64), {})
            self.assertEqual(calls, [])
            with self.assertRaisesRegex(ProviderError, "without a ref"):
                adapter.read(OperationDescriptor("git", "read-only", "git-status"),
                             ResourceScope("git", "github.com", "acme/widgets", "repository",
                                           fingerprint, "refs/heads/main"), {})
            with self.assertRaisesRegex(ProviderError, "diff head"):
                adapter.read(OperationDescriptor("git", "read-only", "git-diff"),
                             ResourceScope("git", "github.com", "acme/widgets", "repository",
                                           fingerprint, new_oid),
                             {"path": "safe.txt", "base": old_oid, "head": old_oid})
            with self.assertRaisesRegex(ProviderError, "log ref"):
                adapter.read(OperationDescriptor("git", "read-only", "git-log"),
                             ResourceScope("git", "github.com", "acme/widgets", "repository",
                                           fingerprint, "refs/heads/main"),
                             {"ref": "refs/heads/other"})

            descriptor = OperationDescriptor("git", "repository-history", "git-push")
            push_scope = ResourceScope("git", "github.com", "acme/widgets", "repository",
                                       fingerprint, "refs/heads/main")
            request = {"remote": "origin", "ref": "refs/heads/main",
                       "expected_old_oid": old_oid, "new_oid": new_oid}
            unsafe = (
                "https://user@github.com/acme/widgets.git",
                "https://github.com:8443/acme/widgets.git",
                "https://github.com/acme/widgets.git?token=x",
                "https://github.com/acme/widgets.git#fragment",
                "git@github.com:acme/widgets.git",
            )
            for remote_url in unsafe:
                calls.clear()

                def unsafe_invoke(**kwargs):
                    argv = list(kwargs["argv"]); calls.append(argv)
                    if "--show-toplevel" in argv:
                        return CommandResult(0, str(repo).encode())
                    if "get-url" in argv:
                        return CommandResult(0, remote_url.encode())
                    return CommandResult(0, b"")

                outcome = GitAdapter(repo, BoundedArgvRunner(unsafe_invoke)).execute(
                    descriptor, push_scope, request, "unsafe-remote"
                )
                self.assertEqual(outcome.state, "failed")
                self.assertFalse(any("ls-remote" in call or "push" in call for call in calls))

    def test_git_rejects_chained_url_rewrite_before_network_or_push(self):
        old_oid = "0123456789abcdef0123456789abcdef01234567"
        new_oid = "fedcba9876543210fedcba9876543210fedcba98"
        with TemporaryDirectory() as directory:
            repo, calls = Path(directory), []

            def invoke(**kwargs):
                argv = list(kwargs["argv"]); calls.append(argv)
                if "--show-toplevel" in argv:
                    return CommandResult(0, str(repo).encode())
                if "remote" in argv and "get-url" in argv:
                    return CommandResult(0, b"https://github.com/acme/widgets.git\n")
                if "config" in argv and "--get-regexp" in argv:
                    return CommandResult(1)
                if "ls-remote" in argv and "--get-url" in argv:
                    # Simulate chained url.*.insteadOf expansion resolving to
                    # an unauthorized final transport.
                    return CommandResult(0, b"https://evil.example/stolen/widgets.git\n")
                return CommandResult(0, b"")

            adapter = GitAdapter(repo, BoundedArgvRunner(invoke))
            push_scope = ResourceScope(
                "git", "github.com", "acme/widgets", "repository",
                git_scope_fingerprint(repo, "github.com", "acme/widgets"), "refs/heads/main",
            )
            outcome = adapter.execute(
                OperationDescriptor("git", "repository-history", "git-push"), push_scope,
                {"remote": "origin", "ref": "refs/heads/main",
                 "expected_old_oid": old_oid, "new_oid": new_oid}, "rewrite-chain",
            )
            self.assertEqual(outcome.state, "failed")
            self.assertFalse(any("ls-remote" in call and "--get-url" not in call for call in calls))
            self.assertFalse(any("push" in call for call in calls))
            adapter.close()

    def test_git_sandboxes_hooks_helpers_and_ssh_commands(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo, bare = root / "repo", root / "remote.git"
            repo.mkdir()

            def git(*args, cwd=repo):
                return subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)

            git("init", "--quiet")
            (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
            git("add", "safe.txt")
            git("-c", "user.name=Tasktra", "-c", "user.email=tasktra@example.invalid",
                "commit", "--quiet", "--no-verify", "-m", "base")
            subprocess.run(["git", "init", "--bare", "--quiet", str(bare)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            hooks = repo / ".git" / "hooks"
            pre_push_sentinel = root / "pre-push-ran"
            reference_sentinel = root / "reference-ran"
            helper_sentinel = root / "credential-helper-ran"
            ssh_sentinel = root / "ssh-command-ran"
            scripts = {
                hooks / "pre-push": pre_push_sentinel,
                hooks / "reference-transaction": reference_sentinel,
                root / "credential-helper.sh": helper_sentinel,
                root / "ssh-command.sh": ssh_sentinel,
            }
            for script, sentinel in scripts.items():
                script.write_text(f'#!/bin/sh\necho ran > "{sentinel.as_posix()}"\nexit 1\n', encoding="utf-8")
                script.chmod(0o755)

            adapter = GitAdapter(repo)
            self.assertEqual(adapter.discover().state, "available")
            head = git("rev-parse", "HEAD").stdout.strip()
            reference = adapter._run(["update-ref", "refs/heads/probe", head])
            self.assertEqual(reference.returncode, 0)
            push = adapter._run([
                "push", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
                "--no-push-option", "--no-signed", "--no-force-if-includes",
                str(bare), "HEAD:refs/heads/main",
            ])
            self.assertEqual(push.returncode, 0, push.stderr.decode("utf-8", "replace"))
            self.assertFalse(pre_push_sentinel.exists())
            self.assertFalse(reference_sentinel.exists())

            git("config", "--local", "credential.helper", f"!{(root / 'credential-helper.sh').as_posix()}")
            git("config", "--local", "core.sshCommand", (root / "ssh-command.sh").as_posix())
            rejected = adapter._verify_transport_url("https://github.com/acme/widgets.git")
            self.assertEqual(rejected.state, "failed")
            self.assertFalse(helper_sentinel.exists())
            self.assertFalse(ssh_sentinel.exists())
            adapter.close()

    def test_github_dispatches_only_the_exact_scoped_workflow_and_branch(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            return CommandResult(0)

        adapter = GitHubCliAdapter(BoundedArgvRunner(invoke))
        workflow_scope = ResourceScope(
            "github", "github.com", "acme/widgets", "workflow",
            ".github/workflows/publish-testpypi.yml", "refs/heads/main",
        )
        descriptor = OperationDescriptor(
            "github", "remote-mutation", "github-workflow-dispatch",
            action="github-workflow-dispatch", resource_scope=workflow_scope, protocol_version=2,
        )
        request = {"workflow": ".github/workflows/publish-testpypi.yml", "ref": "main"}
        self.assertEqual(adapter.execute(descriptor, workflow_scope, request, "dispatch-1").state, "succeeded")
        self.assertEqual(calls[0]["argv"], ("gh", "workflow", "run", ".github/workflows/publish-testpypi.yml", "--repo", "github.com/acme/widgets", "--ref", "main"))
        self.assertEqual(adapter.reconcile(descriptor, workflow_scope, request, "dispatch-1").state, "indeterminate")
        with self.assertRaisesRegex(ProviderError, "exact scoped workflow"):
            adapter.execute(descriptor, workflow_scope, {"workflow": ".github/workflows/other.yml", "ref": "main"}, "dispatch-2")
        with self.assertRaisesRegex(ProviderError, "exact scoped branch"):
            adapter.execute(descriptor, workflow_scope, {"workflow": ".github/workflows/publish-testpypi.yml", "ref": "release"}, "dispatch-3")

    def test_github_has_no_network_health_and_writes_marker_via_stdin(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            if "api" in kwargs["argv"]:
                return CommandResult(0, b'[[{"body":"<!-- tasktra-idempotency:comment-12 -->"}]]')
            return CommandResult(0, b'{"number":12,"title":"safe"}')

        absent = GitHubCliAdapter()
        self.assertEqual(absent.discover().state, "unavailable")
        self.assertEqual(absent.read(OperationDescriptor("github", "read-only", "github-issue-get"), scope("github"), {}).state, "unavailable")
        adapter = GitHubCliAdapter(BoundedArgvRunner(invoke))
        self.assertEqual(adapter.discover().state, "available")
        descriptor = OperationDescriptor("github", "external-communication", "github-issue-comment")
        self.assertEqual(adapter.execute(descriptor, scope("github"), {"body": "hello"}, "comment-12").state, "succeeded")
        self.assertIn(b"tasktra-idempotency:comment-12", calls[-1]["stdin"])
        self.assertNotIn(b"hello", " ".join(calls[-1]["argv"]).encode())
        self.assertEqual(adapter.reconcile(descriptor, scope("github"), {}, "comment-12").state, "reconciled")

    def test_github_lists_bounded_collections_and_creates_marked_issue_and_pr(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            argv = kwargs["argv"]
            if "list" in argv:
                return CommandResult(0, b'[{"number":12,"title":"safe","state":"open","url":"https://example.test/12"}]')
            return CommandResult(0, b'{}')

        adapter = GitHubCliAdapter(BoundedArgvRunner(invoke))
        issue_collection = ResourceScope("github", "github.com", "acme/widgets", "issue")
        pr_collection = ResourceScope("github", "github.com", "acme/widgets", "pull-request")
        issue_list = OperationDescriptor("github", "read-only", "github-issue-list")
        pr_list = OperationDescriptor("github", "read-only", "github-pr-list")
        self.assertEqual(adapter.read(issue_list, issue_collection, {"state": "all", "limit": 7}).state, "succeeded")
        self.assertEqual(adapter.read(pr_list, pr_collection, {}).state, "succeeded")
        self.assertEqual(calls[0]["argv"], ("gh", "issue", "list", "--repo", "github.com/acme/widgets", "--state", "all", "--limit", "7", "--json", "number,title,state,url"))
        self.assertEqual(calls[1]["argv"], ("gh", "pr", "list", "--repo", "github.com/acme/widgets", "--state", "open", "--limit", "20", "--json", "number,title,state,url"))

        create_issue = OperationDescriptor("github", "remote-mutation", "github-issue-create")
        create_pr = OperationDescriptor("github", "remote-mutation", "github-pr-create")
        self.assertEqual(adapter.execute(create_issue, issue_collection, {"title": "Ship it", "body": "details"}, "issue-1").state, "succeeded")
        self.assertEqual(adapter.execute(create_pr, pr_collection, {"title": "Ship PR", "head": "feature/tasktra", "base": "main", "body": "details"}, "pr-1").state, "succeeded")
        self.assertEqual(calls[2]["argv"], ("gh", "issue", "create", "--repo", "github.com/acme/widgets", "--title", "Ship it", "--body-file", "-"))
        self.assertIn(b"tasktra-idempotency:issue-1", calls[2]["stdin"])
        self.assertNotIn(b"details", " ".join(calls[2]["argv"]).encode())
        self.assertEqual(calls[3]["argv"], ("gh", "pr", "create", "--repo", "github.com/acme/widgets", "--title", "Ship PR", "--head", "feature/tasktra", "--base", "main", "--body-file", "-"))
        self.assertIn(b"tasktra-idempotency:pr-1", calls[3]["stdin"])

        with self.assertRaisesRegex(ProviderError, "collection"):
            adapter.execute(create_issue, scope("github"), {"title": "wrong"}, "issue-2")
        with self.assertRaisesRegex(ProviderError, "head branch"):
            adapter.execute(create_pr, pr_collection, {"title": "bad", "head": "refs/heads/nope", "base": "main"}, "pr-2")
        with self.assertRaisesRegex(ProviderError, "limit"):
            adapter.read(issue_list, issue_collection, {"limit": 101})

    def test_github_creates_and_reconciles_exact_repository(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            if "graphql" in kwargs["argv"]:
                return CommandResult(0, b'{"data":{"repository":{"nameWithOwner":"acme/widgets","visibility":"PRIVATE","url":"https://github.com/acme/widgets"}}}')
            return CommandResult(0, b"https://github.com/acme/widgets\n")

        adapter = GitHubCliAdapter(BoundedArgvRunner(invoke))
        repository_scope = ResourceScope("github", "github.com", "acme/widgets", "repository")
        descriptor = OperationDescriptor("github", "remote-mutation", "github-repo-create")
        request = {"visibility": "private", "description": "Tasktra test"}
        self.assertEqual(adapter.execute(descriptor, repository_scope, request, "repo-create-1").state, "succeeded")
        self.assertEqual(calls[0]["argv"], (
            "gh", "repo", "create", "acme/widgets", "--private", "--description", "Tasktra test",
        ))
        self.assertEqual(calls[0]["env"]["GH_HOST"], "github.com")
        self.assertEqual(adapter.reconcile(descriptor, repository_scope, request, "repo-create-1").state, "reconciled")
        self.assertEqual(calls[1]["argv"][:5], ("gh", "api", "graphql", "--hostname", "github.com"))
        absent = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            0, b'{"data":{"repository":null}}'
        ))).reconcile(descriptor, repository_scope, request, "repo-create-absent")
        self.assertEqual(absent.state, "absent")
        not_found = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            1, b'{"data":{"repository":null},"errors":[{"type":"NOT_FOUND","path":["repository"]}]}'
        ))).reconcile(descriptor, repository_scope, request, "repo-create-not-found")
        self.assertEqual(not_found.state, "absent")
        unproven = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            1, b'{"data":{"repository":null},"errors":[{"type":"FORBIDDEN","path":["repository"]}]}'
        ))).reconcile(descriptor, repository_scope, request, "repo-create-forbidden")
        self.assertEqual(unproven.state, "indeterminate")
        with self.assertRaisesRegex(ProviderError, "visibility"):
            adapter.execute(descriptor, repository_scope, {"visibility": "internal", "description": ""}, "repo-create-2")

    def test_github_reconciliation_is_operation_aware_and_never_treats_incomplete_lookup_as_absent(self):
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            argv = kwargs["argv"]
            if "issue" in argv and "list" in argv and "--search" in argv:
                return CommandResult(0, b'[{"number":5,"title":"Ship it","body":"<!-- tasktra-idempotency:issue-1 -->"}]')
            if "pr" in argv and "list" in argv and "--search" in argv:
                return CommandResult(0, b'[{"number":6,"title":"Ship PR","headRefName":"feature/tasktra","baseRefName":"main","body":"<!-- tasktra-idempotency:pr-1 -->"}]')
            return CommandResult(0, b'[]')

        adapter = GitHubCliAdapter(BoundedArgvRunner(invoke))
        issue_collection = ResourceScope("github", "github.com", "acme/widgets", "issue")
        pr_collection = ResourceScope("github", "github.com", "acme/widgets", "pull-request")
        issue_create = OperationDescriptor("github", "remote-mutation", "github-issue-create")
        pr_create = OperationDescriptor("github", "remote-mutation", "github-pr-create")
        issue = adapter.reconcile(issue_create, issue_collection, {"title": "Ship it"}, "issue-1")
        pull_request = adapter.reconcile(pr_create, pr_collection, {"title": "Ship PR", "head": "feature/tasktra", "base": "main"}, "pr-1")
        self.assertEqual(issue.state, "reconciled")
        self.assertEqual(pull_request.state, "reconciled")
        self.assertEqual(calls[0]["argv"], ("gh", "issue", "list", "--repo", "github.com/acme/widgets", "--state", "all", "--limit", "1000", "--search", "<!-- tasktra-idempotency:issue-1 --> in:body", "--json", "number,title,state,url,body"))
        self.assertEqual(calls[1]["argv"], ("gh", "pr", "list", "--repo", "github.com/acme/widgets", "--state", "all", "--limit", "1000", "--search", "<!-- tasktra-idempotency:pr-1 --> in:body", "--json", "number,title,state,url,body,headRefName,baseRefName"))

        comment_scope = scope("github")
        duplicate = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            0, b'[[{"body":"<!-- tasktra-idempotency:comment-1 -->"},{"body":"<!-- tasktra-idempotency:comment-1 -->"}]]'
        ))).reconcile(OperationDescriptor("github", "external-communication", "github-issue-comment"), comment_scope, {}, "comment-1")
        self.assertEqual(duplicate.state, "conflict")
        self.assertIn("duplicate", duplicate.summary)
        absent = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0, b'[[]]'))).reconcile(OperationDescriptor("github", "external-communication", "github-issue-comment"), comment_scope, {}, "comment-2")
        self.assertEqual(absent.state, "indeterminate")
        self.assertIn("no marker", absent.summary)
        timeout = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(124, timed_out=True))).reconcile(OperationDescriptor("github", "external-communication", "github-issue-comment"), comment_scope, {}, "comment-3")
        self.assertEqual(timeout.state, "indeterminate")
        malformed = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0, b'{not-json'))).reconcile(OperationDescriptor("github", "external-communication", "github-issue-comment"), comment_scope, {}, "comment-4")
        self.assertEqual(malformed.state, "indeterminate")

    def test_github_status_reconciliation_binds_commit_context_and_marker(self):
        oid = "0123456789abcdef0123456789abcdef01234567"
        status_scope = ResourceScope("github", "github.com", "acme/widgets", "commit-status", None, oid)
        descriptor = OperationDescriptor("github", "remote-mutation", "github-status-set")
        request = {"state": "success", "context": "ci/tasktra", "body": "passed"}
        marker = "<!-- tasktra-idempotency:status-1 -->"
        calls = []

        def invoke(**kwargs):
            calls.append(kwargs)
            if "GET" in kwargs["argv"]:
                return CommandResult(0, b'{}')
            if "statuses" in " ".join(kwargs["argv"]):
                return CommandResult(0, json.dumps([[{"state": "success", "context": "ci/tasktra", "description": f"passed {marker}"}]]).encode())
            return CommandResult(0, b"{}")

        adapter = GitHubCliAdapter(BoundedArgvRunner(invoke))
        self.assertEqual(adapter.execute(descriptor, status_scope, request, "status-1").state, "succeeded")
        self.assertEqual(adapter.reconcile(descriptor, status_scope, request, "status-1").state, "reconciled")
        self.assertIn(marker.encode(), calls[0]["stdin"])
        self.assertTrue(any(item.endswith(f"commits/{oid}/statuses?per_page=100") for item in calls[1]["argv"]))
        self.assertIn("--paginate", calls[1]["argv"])
        self.assertIn("--slurp", calls[1]["argv"])

        absent = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0, b'[[]]'))).reconcile(descriptor, status_scope, request, "status-1")
        self.assertEqual(absent.state, "indeterminate")
        conflict = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0, json.dumps([[{"state": "success", "context": "ci/tasktra", "description": marker}, {"state": "success", "context": "ci/tasktra", "description": marker}] ]).encode()))).reconcile(descriptor, status_scope, request, "status-1")
        self.assertEqual(conflict.state, "conflict")
        timeout = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(124, timed_out=True))).reconcile(descriptor, status_scope, request, "status-1")
        self.assertEqual(timeout.state, "indeterminate")

    def test_github_passes_minimal_external_auth_environment_without_receipt_leakage(self):
        captured = []

        def invoke(**kwargs):
            captured.append(kwargs)
            return CommandResult(0, b'{"number":12,"title":"safe","state":"open","url":"https://example.test/12"}')

        with patch.dict("os.environ", {
            "PATH": "safe-path", "GH_TOKEN": "primary-secret", "GITHUB_TOKEN": "fallback-secret",
            "GH_ENTERPRISE_TOKEN": "enterprise-secret", "GH_HOST": "github.example", "GH_CONFIG_DIR": "C:/safe/config",
            "SystemRoot": "C:/Windows", "TEMP": "C:/safe/temp", "TMP": "C:/safe/tmp",
            "UNRELATED_SECRET": "must-not-pass",
        }, clear=True):
            output = GitHubCliAdapter(BoundedArgvRunner(invoke)).read(
                OperationDescriptor("github", "read-only", "github-issue-get"), scope("github"), {}
            )
        environment = captured[0]["env"]
        self.assertEqual(environment["GH_TOKEN"], "primary-secret")
        self.assertEqual(environment["GH_ENTERPRISE_TOKEN"], "enterprise-secret")
        self.assertEqual(environment["GH_HOST"], "github.example")
        self.assertEqual(environment["GH_CONFIG_DIR"], "C:/safe/config")
        self.assertEqual(environment["SystemRoot"], "C:/Windows")
        self.assertEqual(environment["TEMP"], "C:/safe/temp")
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertNotIn("primary-secret", str(output.to_dict()))
        self.assertNotIn("enterprise-secret", str(output.to_dict()))

        captured.clear()
        with patch.dict("os.environ", {"PATH": "safe-path", "GITHUB_TOKEN": "fallback-secret"}, clear=True):
            GitHubCliAdapter(BoundedArgvRunner(invoke)).read(
                OperationDescriptor("github", "read-only", "github-issue-get"), scope("github"), {}
            )
        self.assertEqual(captured[0]["env"]["GH_TOKEN"], "fallback-secret")

    def test_github_effect_capabilities_require_their_exact_effect_class(self):
        adapter = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0, b"{}")))
        issue_collection = ResourceScope("github", "github.com", "acme/widgets", "issue")
        with self.assertRaisesRegex(ProviderError, "effect class"):
            adapter.execute(OperationDescriptor("github", "remote-mutation", "github-issue-comment"), scope("github"), {"body": "hello"}, "x-1")
        with self.assertRaisesRegex(ProviderError, "effect class"):
            adapter.execute(OperationDescriptor("github", "external-communication", "github-issue-create"), issue_collection, {"title": "hello"}, "x-2")
        with self.assertRaisesRegex(ProviderError, "effect class"):
            adapter.read(OperationDescriptor("github", "external-communication", "github-issue-list"), issue_collection, {})

    def test_github_exact_scope_reconciliation_and_post_dispatch_uncertainty(self):
        comment = OperationDescriptor("github", "external-communication", "github-issue-comment")
        with self.assertRaisesRegex(ProviderError, "exact issue"):
            GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0))).execute(
                comment, ResourceScope("github", "github.com", "acme/widgets", "pull-request", "12"),
                {"body": "hello"}, "wrong-kind",
            )
        status = OperationDescriptor("github", "remote-mutation", "github-status-set")
        with self.assertRaisesRegex(ProviderError, "commit-status"):
            GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(0))).execute(
                status, ResourceScope("github", "github.com", "acme/widgets", "issue", "12"),
                {"state": "success", "context": "ci/tasktra"}, "wrong-status-kind",
            )

        exact_marker = "<!-- tasktra-idempotency:issue-conflict -->"
        issue_collection = ResourceScope("github", "github.com", "acme/widgets", "issue")
        conflict = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            0, json.dumps([{"title": "Other", "body": exact_marker}]).encode()
        ))).reconcile(OperationDescriptor("github", "remote-mutation", "github-issue-create"),
                      issue_collection, {"title": "Expected"}, "issue-conflict")
        self.assertEqual(conflict.state, "conflict")

        oid = "0123456789abcdef0123456789abcdef01234567"
        status_scope = ResourceScope("github", "github.com", "acme/widgets", "commit-status", None, oid)
        incomplete = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            0, b'[[{"description":"<!-- tasktra-idempotency:status-cut -->"}]]', output_limited=True
        ))).reconcile(status, status_scope, {"state": "success", "context": "ci/tasktra"}, "status-cut")
        self.assertEqual(incomplete.state, "indeterminate")
        malformed_history = GitHubCliAdapter(BoundedArgvRunner(lambda **_: CommandResult(
            0, b'[[{"state":"success","context":"ci/tasktra","description":"<!-- tasktra-idempotency:status-bad -->"},"truncated"]]'
        ))).reconcile(status, status_scope, {"state": "success", "context": "ci/tasktra"}, "status-bad")
        self.assertEqual(malformed_history.state, "indeterminate")

        for raw in (
            CommandResult(1), CommandResult(124, timed_out=True),
            CommandResult(0, output_limited=True), CommandResult(0, input_uncertain=True),
        ):
            outcome = GitHubCliAdapter(BoundedArgvRunner(lambda raw=raw, **_: raw)).execute(
                comment, scope("github"), {"body": "hello"}, "uncertain-write"
            )
            self.assertEqual(outcome.state, "indeterminate")
        not_dispatched = GitHubCliAdapter(BoundedArgvRunner(
            lambda **_: CommandResult(127, dispatched=False)
        )).execute(comment, scope("github"), {"body": "hello"}, "not-dispatched")
        self.assertEqual(not_dispatched.state, "failed")

    def test_jira_injected_connector_covers_success_throttle_timeout_malformed_and_marker(self):
        captured = []

        def success(payload):
            captured.append(payload)
            return result(items=[{"key": "ABC-12"}])

        descriptor = OperationDescriptor("jira", "remote-mutation", "jira-transition")
        adapter = JiraConnectorAdapter(success)
        self.assertEqual(adapter.execute(descriptor, jira_scope(), {"transition": "done"}, "jira-12").state, "succeeded")
        self.assertEqual(captured[0]["idempotency_marker"], "tasktra-idempotency:jira-12")
        self.assertEqual(JiraConnectorAdapter().discover().state, "unavailable")
        self.assertEqual(JiraConnectorAdapter().read(OperationDescriptor("jira", "read-only", "jira-issue-get"), jira_scope(), {}).state, "unavailable")

        class Throttled(Exception):
            pass

        request = {"transition": "done"}
        self.assertEqual(JiraConnectorAdapter(lambda _: (_ for _ in ()).throw(Throttled())).execute(descriptor, jira_scope(), request, "x-1").state, "indeterminate")
        self.assertEqual(JiraConnectorAdapter(lambda _: (_ for _ in ()).throw(TimeoutError())).execute(descriptor, jira_scope(), request, "x-2").state, "indeterminate")
        self.assertEqual(JiraConnectorAdapter(lambda _: {"bad": True}).execute(descriptor, jira_scope(), request, "x-3").state, "indeterminate")
        read_descriptor = OperationDescriptor("jira", "read-only", "jira-issue-get")
        self.assertEqual(
            JiraConnectorAdapter(lambda _: {"bad": True}).read(read_descriptor, jira_scope(), {}).state,
            "failed",
        )

    def test_jira_reconciliation_is_typed_read_only_connector_work(self):
        captured = []

        def reconcile(payload):
            captured.append(payload)
            return result("reconciled", "Jira marker was found", [{"key": "ABC-12"}])

        scope_jira = jira_scope()
        transition = OperationDescriptor("jira", "remote-mutation", "jira-transition")
        output = JiraConnectorAdapter(reconcile).reconcile(transition, scope_jira, {"transition": "done"}, "jira-12")
        self.assertEqual(output.state, "reconciled")
        self.assertTrue(captured[0]["reconcile_only"])
        self.assertEqual(captured[0]["idempotency_marker"], "tasktra-idempotency:jira-12")
        self.assertEqual(captured[0]["operation"], transition.to_dict())
        self.assertEqual(captured[0]["scope"], scope_jira.to_dict())
        self.assertEqual(captured[0]["request"], {"transition": "done"})
        absent = JiraConnectorAdapter(lambda _: result("absent", "marker not observed")).reconcile(
            transition, scope_jira, {"transition": "done"}, "jira-absent-race"
        )
        self.assertEqual(absent.state, "indeterminate")
        self.assertIn("cannot prove", absent.summary)

        comment = OperationDescriptor("jira", "external-communication", "jira-comment")
        self.assertEqual(JiraConnectorAdapter(lambda _: result()).execute(comment, scope_jira, {"body": "hello"}, "jira-comment").state, "succeeded")
        with self.assertRaisesRegex(ProviderError, "effect class"):
            JiraConnectorAdapter(reconcile).execute(OperationDescriptor("jira", "remote-mutation", "jira-comment"), scope_jira, {"body": "hello"}, "wrong-effect")
        with self.assertRaisesRegex(ProviderError, "provider"):
            JiraConnectorAdapter(reconcile).read(OperationDescriptor("github", "read-only", "jira-issue-get"), scope_jira, {})
        with self.assertRaisesRegex(ProviderError, "provider"):
            JiraConnectorAdapter(reconcile).read(OperationDescriptor("jira", "read-only", "jira-issue-get"), scope("github"), {})

    def test_jira_targets_bounds_and_connector_uncertainty_are_fail_closed(self):
        transition = OperationDescriptor("jira", "remote-mutation", "jira-transition")
        adapter = JiraConnectorAdapter(lambda _: result())
        with self.assertRaisesRegex(ProviderError, "issue differs"):
            adapter.execute(transition, jira_scope(), {"issue": "ABC-13", "transition": "done"}, "jira-target")
        with self.assertRaisesRegex(ProviderError, "project differs"):
            adapter.execute(transition, jira_scope(), {"project": "XYZ", "transition": "done"}, "jira-project")
        with self.assertRaisesRegex(ProviderError, "transition"):
            adapter.execute(transition, jira_scope(), {"transition": "x" * 101}, "jira-long-transition")
        comment = OperationDescriptor("jira", "external-communication", "jira-comment")
        with self.assertRaisesRegex(ProviderError, "body"):
            adapter.execute(comment, jira_scope(), {"body": "x" * 4001}, "jira-long-body")

        request = {"transition": "done"}
        for error in (ConnectionError("down"), OSError("broken"), RuntimeError("unknown")):
            outcome = JiraConnectorAdapter(
                lambda _, error=error: (_ for _ in ()).throw(error)
            ).execute(transition, jira_scope(), request, "jira-uncertain")
            self.assertEqual(outcome.state, "indeterminate")
        self.assertEqual(
            JiraConnectorAdapter(lambda _: ["not", "a", "mapping"]).execute(
                transition, jira_scope(), request, "jira-malformed"
            ).state,
            "indeterminate",
        )
        self.assertEqual(
            JiraConnectorAdapter(lambda _: {"bad": True}).reconcile(
                transition, jira_scope(), request, "jira-malformed-reconcile"
            ).state,
            "indeterminate",
        )


if __name__ == "__main__":
    unittest.main()
