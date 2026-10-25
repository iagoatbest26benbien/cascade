"""Real temporary file/Git sources and synthetic, subprocess-stubbed GitHub reads."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "sources.py"
module_spec = importlib.util.spec_from_file_location("ticket_sources", SCRIPT)
sources = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(sources)
SHA = "a" * 40
OTHER_SHA = "b" * 40
STAMP = "2026-10-24T12:00:00Z"


def issue():
    return {"id": 123, "number": 7, "title": "Requirement", "body": "Ship the full contract.",
            "state": "open", "updated_at": STAMP,
            "labels": [{"id": 2, "name": "ready"}, {"id": 1, "name": "required"}],
            "milestone": {"id": 3, "number": 1, "title": "v1", "state": "open", "description": None,
                          "updated_at": STAMP, "due_on": None}}


def suite(number=1):
    return {"id": number, "head_sha": SHA, "status": "completed", "conclusion": "success",
            "created_at": STAMP, "updated_at": STAMP}


def run(number=11, suite_id=1):
    return {"id": number, "head_sha": SHA, "status": "completed", "conclusion": "success",
            "name": "unit", "started_at": STAMP, "completed_at": STAMP, "external_id": "attempt-1",
            "check_suite": {"id": suite_id}, "details_url": "https://example.invalid/run/1/attempts/1"}


def status(number=21):
    return {"id": number, "state": "success", "context": "legacy-ci", "description": "Passed",
            "target_url": "https://example.invalid/status/1", "created_at": STAMP, "updated_at": STAMP}


class GithubStub:
    """Exact endpoint table. A missing request fails instead of contacting a service."""

    def __init__(self, table):
        self.table = copy.deepcopy(table)
        self.calls = []

    def __call__(self, argv, **kwargs):
        assert argv[:7] == ["gh", "api", "--hostname", "github.com", "--method", "GET", "-H"]
        assert argv[7] == "Accept: application/vnd.github+json"
        assert len(argv) == 9
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert 0 < kwargs["timeout"] <= 10
        assert not kwargs.get("shell", False)
        assert "GH_DEBUG" not in kwargs["env"]
        assert not any(name.startswith("GIT_TRACE") for name in kwargs["env"])
        endpoint = argv[-1]
        self.calls.append(endpoint)
        if endpoint not in self.table:
            raise AssertionError("Unexpected stub request: " + endpoint)
        result = self.table[endpoint]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result()
        if isinstance(result, subprocess.CompletedProcess):
            return result
        return subprocess.CompletedProcess(argv, 0, json.dumps(result).encode(), b"")


def checks_table():
    base = "repos/owner/repo"
    return {f"{base}/commits/main": {"sha": SHA},
            f"{base}/commits/{SHA}/check-suites?per_page=100&page=1": {"total_count": 1, "check_suites": [suite()]},
            f"{base}/check-suites/1/check-runs?filter=all&per_page=100&page=1": {"total_count": 1, "check_runs": [run()]},
            f"{base}/commits/{SHA}/statuses?per_page=100&page=1": [status()]}


class SourcesFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-tests-")
        self.root = Path(self.temp.name)
        self.file = self.root / "requirements.txt"
        self.file.write_bytes(b"First requirement\n")
        self.spec = {"id": "ticket", "role": "ticket", "kind": "file", "path": self.file.name}

    def tearDown(self):
        self.temp.cleanup()

    def evaluate(self, spec):
        return sources.evaluate([spec], self.root)[0]

    def cli(self, command, spec):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), command, "--spec", json.dumps(spec),
                               "--root", str(self.root)], capture_output=True, text=True, timeout=10)


class SourcesTests(SourcesFixture):
    def test_file_bytes_change_without_changing_expected_descriptor(self):
        self.spec["notes"] = {"source": "fixture://original"}
        original = copy.deepcopy(self.spec)
        captured = sources.capture(self.spec, self.root)
        self.assertEqual(self.spec, original)
        self.assertEqual(captured["fingerprint"], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertRegex(captured["observed_at"], r"T.*\+00:00$")
        captured["notes"]["source"] = "fixture://copy"
        self.assertEqual(self.spec["notes"]["source"], "fixture://original")
        self.assertEqual(self.evaluate(captured)["status"], "unchanged")
        self.file.write_bytes(b"Other requirement\n")
        result = self.evaluate(captured)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["expected"], captured["fingerprint"])
        self.assertNotEqual(result["observed"], captured["fingerprint"])
        self.assertNotIn("Other requirement", json.dumps(result))

    def test_file_paths_absolute_and_tracker_relative_with_no_mtime_freshness(self):
        first = sources.capture(self.spec, self.root)
        second = sources.capture(dict(self.spec, path=str(self.file)), self.root)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        metadata = self.file.stat()
        self.file.write_bytes(b"Other requirement\n")
        os.utime(self.file, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        self.assertEqual(self.evaluate(first)["status"], "changed")

    def test_unreadable_missing_nonregular_and_oversized_files_are_unknown(self):
        captured = sources.capture(self.spec, self.root)
        self.file.unlink()
        self.assertEqual(self.evaluate(captured)["status"], "unknown")
        self.file.mkdir()
        self.assertEqual(self.evaluate(captured)["status"], "unknown")
        self.file.rmdir()
        os.mkfifo(self.file)
        self.assertEqual(self.evaluate(captured)["status"], "unknown")
        self.file.unlink()
        self.file.write_bytes(b"Data")
        with patch.object(sources, "MAX_FILE_BYTES", 2):
            self.assertEqual(self.evaluate(captured)["status"], "unknown")
        with patch.object(sources.os, "open", side_effect=PermissionError("private-token")):
            result = self.evaluate(captured)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("private-token", json.dumps(result))

    def test_file_changed_during_observation_is_unknown(self):
        captured = sources.capture(self.spec, self.root)
        original = sources.os.fstat
        calls = []

        def changing_stat(fd):
            calls.append(fd)
            if len(calls) == 2:
                self.file.write_bytes(b"Replacement requirement\n")
            return original(fd)

        with patch.object(sources.os, "fstat", side_effect=changing_stat):
            self.assertEqual(self.evaluate(captured)["status"], "unknown")

    def test_missing_invalid_fingerprints_and_duplicate_ids_never_pass(self):
        captured = sources.capture(self.spec, self.root)
        for fingerprint in (None, "", "a" * 40, "A" * 64, "private-token", [], {}):
            with self.subTest(fingerprint=fingerprint):
                result = self.evaluate(dict(captured, fingerprint=fingerprint))
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["expected"])
        with patch.object(sources, "capture", side_effect=AssertionError("duplicate must not read")):
            results = sources.evaluate([captured, copy.deepcopy(captured)], self.root)
        self.assertEqual([row["status"] for row in results], ["unknown", "unknown"])
        self.assertTrue(all("unique" in row["reason"] for row in results))

    def test_malformed_or_unsupported_sources_are_unknown(self):
        captured = sources.capture(self.spec, self.root)
        invalid = [None, [], 1, {}, {"id": []}, dict(captured, id=""), dict(captured, id="x\nsecret"),
                   dict(captured, role=[]), dict(captured, kind={}), dict(captured, kind="web"),
                   dict(captured, path=None), dict(captured, path="nul\x00path"),
                   dict(captured, notes=object()), dict(captured, notes=float("nan")),
                   dict(captured, id="invalid\ud800"), dict(captured, notes="invalid\ud800")]
        results = sources.evaluate(invalid, self.root)
        self.assertTrue(all(row["status"] == "unknown" for row in results))
        for value in ([], {}, None, [captured] * (sources.MAX_SOURCES + 1)):
            rows = sources.evaluate(value, self.root)
            self.assertTrue(rows)
            self.assertEqual(rows[0]["status"], "unknown")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sources.capture(value, self.root)

    def test_cli_capture_and_check_exit_states_and_safe_failure(self):
        result = self.cli("capture", self.spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(result.stdout)
        result = self.cli("check", captured)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.file.write_text("Changed")
        result = self.cli("check", [captured])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)[0]["status"], "changed")
        self.file.unlink()
        result = self.cli("check", captured)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)[0]["status"], "unknown")
        self.assertNotEqual(self.cli("check", []).returncode, 0)
        result = self.cli("capture", dict(self.spec, path="private-token"))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("private-token", result.stderr + result.stdout)


class GitFixture(SourcesFixture):
    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def setUp(self):
        super().setUp()
        self.repo = self.root / "code"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic test")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.code = self.repo / "code.txt"
        self.code.write_text("v1")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "initial")
        self.git_spec = {"id": "code", "role": "code", "kind": "git", "path": "code", "ref": "HEAD"}


class GitTests(GitFixture):
    def test_real_git_head_changes_with_commit_and_does_not_modify_worktree(self):
        captured = sources.capture(self.git_spec, self.root)
        self.assertEqual(self.evaluate(captured)["status"], "unchanged")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.code.write_text("v2")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "next")
        self.assertEqual(self.evaluate(captured)["status"], "changed")

    def test_head_rejects_tracked_staged_and_untracked_changes(self):
        captured = sources.capture(self.git_spec, self.root)
        self.code.write_text("dirty")
        self.assertEqual(self.evaluate(captured)["status"], "unknown")
        self.git("add", "code.txt")
        self.assertEqual(self.evaluate(captured)["status"], "unknown")
        self.git("commit", "-qm", "record")
        captured = sources.capture(self.git_spec, self.root)
        (self.repo / "untracked.txt").write_text("new")
        self.assertEqual(self.evaluate(captured)["status"], "unknown")

    def test_pinned_commit_is_a_commit_observation_despite_worktree_edits(self):
        commit = self.git("rev-parse", "HEAD")
        captured = sources.capture(dict(self.git_spec, ref=commit), self.root)
        self.code.write_text("uncommitted")
        self.assertEqual(self.evaluate(captured)["status"], "unchanged")
        self.assertEqual(captured["fingerprint"], sources.digest({"kind": "git", "commit": commit}))

    def test_head_rejects_suppressing_index_flags_and_preserves_pinned_refs(self):
        head = sources.capture(self.git_spec, self.root)
        pinned = sources.capture(dict(self.git_spec, ref=self.git("rev-parse", "HEAD")), self.root)
        for flags in (("--assume-unchanged",), ("--skip-worktree",),
                      ("--assume-unchanged", "--skip-worktree")):
            with self.subTest(flags=flags):
                self.code.write_text("v1")
                self.git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "code.txt")
                self.git("update-index", *flags, "code.txt")
                self.assertEqual(self.evaluate(head)["status"], "unknown")
                self.code.write_text("changed bytes hidden by index flag")
                self.assertEqual(self.git("status", "--porcelain=v1"), "")
                self.assertNotEqual(self.git("show", "HEAD:code.txt"), self.code.read_text())
                index = self.repo / ".git/index"
                before = index.read_bytes()
                result = self.evaluate(head)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["observed"])
                self.assertIn("index", result["reason"])
                with self.assertRaises(ValueError):
                    sources.capture(self.git_spec, self.root)
                self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
                self.assertEqual(sources.capture(pinned, self.root)["fingerprint"], pinned["fingerprint"])
                self.assertEqual(index.read_bytes(), before)

    def test_head_in_subdirectory_checks_index_flags_outside_that_directory(self):
        nested = self.repo / "nested"
        nested.mkdir()
        captured = sources.capture(dict(self.git_spec, path=str(nested)), self.root)
        self.git("update-index", "--assume-unchanged", "code.txt")
        self.code.write_text("changed outside source directory")
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        self.assertEqual(self.evaluate(captured)["status"], "unknown")

    def test_head_disables_fsmonitor_that_falsely_reports_unchanged(self):
        captured = sources.capture(self.git_spec, self.root)
        pinned = sources.capture(dict(self.git_spec, ref=self.git("rev-parse", "HEAD")), self.root)
        hook = self.repo / ".git/hooks/synthetic-fsmonitor"
        hook.write_text("#!/bin/sh\nprintf 'synthetic-token\\0'\n")
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        self.git("config", "core.fsmonitorHookVersion", "2")
        self.git("status", "--porcelain=v1")
        self.git("update-index", "--fsmonitor-valid", "code.txt")
        self.assertEqual(self.evaluate(captured)["status"], "unchanged")
        self.code.write_text("changed bytes hidden by fsmonitor")
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        self.assertNotEqual(self.git("show", "HEAD:code.txt"), self.code.read_text())
        index = self.repo / ".git/index"
        before = index.read_bytes()
        config = (self.repo / ".git/config").read_bytes()
        result = self.evaluate(captured)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("dirty", result["reason"])
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((self.repo / ".git/config").read_bytes(), config)

    def test_head_rejects_stat_shortcuts_without_mutating_index(self):
        captured = sources.capture(self.git_spec, self.root)
        pinned = sources.capture(dict(self.git_spec, ref=self.git("rev-parse", "HEAD")), self.root)
        self.git("config", "core.trustctime", "false")
        self.git("config", "core.checkStat", "minimal")
        os.utime(self.code, (1, 1))
        self.git("update-index", "--refresh")
        metadata = self.code.stat()
        self.code.write_text("v2")
        os.utime(self.code, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        index = self.repo / ".git/index"
        before = index.read_bytes()
        self.assertEqual(self.evaluate(captured)["status"], "unknown")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertEqual(index.read_bytes(), before)

    def test_head_rejects_reduced_stat_configuration_and_pinned_refs_remain_valid(self):
        captured = sources.capture(self.git_spec, self.root)
        pinned = sources.capture(dict(self.git_spec, ref=self.git("rev-parse", "HEAD")), self.root)
        for key, value, default in (("core.ignoreStat", "true", "false"),
                                    ("core.trustctime", "false", "true"),
                                    ("core.checkStat", "minimal", "default")):
            with self.subTest(key=key):
                self.git("config", key, value)
                index = self.repo / ".git/index"
                before = index.read_bytes()
                result = self.evaluate(captured)
                self.assertEqual(result["status"], "unknown")
                self.assertIn("configuration", result["reason"])
                with self.assertRaises(ValueError):
                    sources.capture(self.git_spec, self.root)
                self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
                self.assertEqual(index.read_bytes(), before)
                self.assertEqual(self.git("config", "--get", key), value)
                self.git("config", key, default)

    def test_head_overrides_config_that_hides_executable_mode_changes(self):
        captured = sources.capture(self.git_spec, self.root)
        self.git("config", "core.fileMode", "false")
        self.code.chmod(self.code.stat().st_mode ^ 0o100)
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        self.assertEqual(self.evaluate(captured)["status"], "unknown")

    def test_head_handles_nul_delimited_index_paths(self):
        unusual = self.repo / "h name\nS other.txt"
        unusual.write_text("tracked")
        self.git("add", "--", unusual.name)
        self.git("commit", "-qm", "unusual path")
        captured = sources.capture(self.git_spec, self.root)
        self.assertEqual(self.evaluate(captured)["status"], "unchanged")

    def test_declared_nested_checkout_is_honored_despite_inherited_git_context(self):
        inner = self.repo / "inner"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(inner)],
                       check=True, capture_output=True, timeout=10)
        with (self.repo / ".git/info/exclude").open("a") as stream:
            stream.write("\ninner/\n")
        captured = sources.capture(dict(self.git_spec, path=str(inner)), self.root)
        (inner / "code.txt").write_text("modified declared source")
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        dirty = subprocess.run(["git", "-C", str(inner), "status", "--porcelain=v1"],
                               check=True, capture_output=True, timeout=10)
        self.assertTrue(dirty.stdout)
        pointers = {"GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(self.repo),
                    "GIT_INDEX_FILE": str(self.repo / ".git/index")}
        auth = {"GH_TOKEN": "fixture-gh-token", "GITHUB_TOKEN": "fixture-github-token",
                "GIT_SSH_COMMAND": "fixture-ssh-command", "GIT_ASKPASS": "fixture-askpass",
                "SSH_AUTH_SOCK": "fixture-agent"}
        paths = [repo / ".git" / name for repo in (self.repo, inner) for name in ("index", "config")]
        before = {path: path.read_bytes() for path in paths}
        environments = []
        run = subprocess.run

        def observe(argv, **kwargs):
            environments.append(kwargs["env"])
            return run(argv, **kwargs)

        with patch.dict(os.environ, dict(pointers, **auth)), patch.object(sources.subprocess, "run", side_effect=observe):
            result = self.evaluate(captured)
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(all(os.environ[key] == value for key, value in pointers.items()))
        self.assertTrue(environments)
        self.assertTrue(all(not (pointers.keys() & env.keys()) for env in environments))
        self.assertTrue(all(all(env[key] == value for key, value in auth.items()) for env in environments))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_missing_ref_nonrepo_and_option_like_ref_are_unknown(self):
        captured = sources.capture(self.git_spec, self.root)
        for changes in ({"ref": "missing"}, {"ref": "--help"}, {"path": str(self.root)}, {"ref": None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.evaluate(dict(captured, **changes))["status"], "unknown")

    def test_head_requires_worktree_and_rejects_racing_revision(self):
        bare = self.root / "bare.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(bare)],
                       check=True, capture_output=True, timeout=10)
        spec = dict(self.git_spec, path=str(bare), fingerprint="f" * 64)
        self.assertEqual(self.evaluate(spec)["status"], "unknown")
        captured = sources.capture(self.git_spec, self.root)
        outputs = ["a" * 40 + "\n", "true\n", str(self.repo) + "\n", str(self.repo) + "\n",
                   "a" * 40 + "\n", "H 100644 " + "a" * 40 + " 0\tcode.txt\0",
                   "false\n", "true\n", "default\n", "", "", "b" * 40 + "\n"]
        with patch.object(sources.Reader, "run", side_effect=outputs):
            result = self.evaluate(captured)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("changed during observation", result["reason"])


class SubmoduleTests(GitFixture):
    def at(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def dependency(self, name):
        repo = self.root / name
        repo.mkdir()
        self.at(repo, "init", "-q")
        self.at(repo, "config", "user.name", "Synthetic submodule")
        self.at(repo, "config", "user.email", "fixture@example.invalid")
        self.at(repo, "config", "commit.gpgsign", "false")
        (repo / "main.txt").write_text("v1")
        self.at(repo, "add", "main.txt")
        self.at(repo, "commit", "-qm", "initial")
        return repo

    def add_module(self, parent, source, name):
        self.at(parent, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", "--", str(source), name)
        self.at(parent, "-c", "user.name=Synthetic submodule", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qam", "add local module")
        return parent / name

    def modules(self, nested=False, name="library"):
        source = self.dependency("dependency")
        if nested:
            leaf = self.dependency("leaf")
            self.add_module(source, leaf, "nested leaf")
        module = self.add_module(self.repo, source, name)
        if nested:
            self.at(self.repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--quiet")
        return [self.repo, module] + ([module / "nested leaf"] if nested else [])

    def metadata(self, repos):
        result = {}
        for repo in repos:
            gitdir = Path(self.at(repo, "rev-parse", "--absolute-git-dir"))
            for name in ("index", "config"):
                path = gitdir / name
                result[path] = path.read_bytes()
        return result

    def versions(self):
        head = sources.capture(self.git_spec, self.root)
        pinned = sources.capture(dict(self.git_spec, ref=self.git("rev-parse", "HEAD")), self.root)
        return head, pinned

    def test_clean_one_level_and_nested_modules_preserve_metadata_and_pinned_refs(self):
        repos = self.modules(nested=True)
        before = self.metadata(repos)
        head, pinned = self.versions()
        self.assertEqual(self.evaluate(head)["status"], "unchanged")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertEqual(self.metadata(repos), before)

    def test_hidden_index_flags_in_one_level_and_nested_modules_block_head(self):
        repos = self.modules(nested=True)
        head, pinned = self.versions()
        for repo in repos[1:]:
            for flags in (("--assume-unchanged",), ("--skip-worktree",),
                          ("--assume-unchanged", "--skip-worktree")):
                with self.subTest(repo=repo.name, flags=flags):
                    self.at(repo, "update-index", *flags, "main.txt")
                    (repo / "main.txt").write_text("hidden changed tracked bytes")
                    self.assertEqual(self.at(repo, "status", "--porcelain=v1"), "")
                    self.assertEqual(self.git("status", "--porcelain=v1"), "")
                    self.assertNotEqual(self.at(repo, "show", "HEAD:main.txt"), (repo / "main.txt").read_text())
                    before = self.metadata(repos)
                    result = self.evaluate(head)
                    self.assertEqual(result["status"], "unknown")
                    self.assertIsNone(result["observed"])
                    with self.assertRaises(ValueError):
                        sources.capture(self.git_spec, self.root)
                    self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
                    self.assertEqual(self.metadata(repos), before)
                    (repo / "main.txt").write_text("v1")
                    self.at(repo, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "main.txt")

    def test_suppressing_configuration_in_each_module_blocks_head(self):
        repos = self.modules(nested=True)
        head, pinned = self.versions()
        for repo in repos[1:]:
            for key, value, default in (("core.ignoreStat", "true", "false"),
                                        ("core.trustctime", "false", "true"),
                                        ("core.checkStat", "minimal", "default")):
                with self.subTest(repo=repo.name, key=key):
                    self.at(repo, "config", key, value)
                    before = self.metadata(repos)
                    self.assertEqual(self.evaluate(head)["status"], "unknown")
                    self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
                    self.assertEqual(self.metadata(repos), before)
                    self.at(repo, "config", key, default)

    def test_nested_fsmonitor_cannot_hide_changed_bytes(self):
        repos = self.modules(nested=True)
        leaf = repos[-1]
        head, pinned = self.versions()
        hook = self.root / "synthetic-fsmonitor"
        hook.write_text("#!/bin/sh\nprintf 'synthetic-token\\0'\n")
        hook.chmod(0o700)
        self.at(leaf, "config", "core.fsmonitor", str(hook))
        self.at(leaf, "config", "core.fsmonitorHookVersion", "2")
        self.at(leaf, "status", "--porcelain=v1")
        self.at(leaf, "update-index", "--fsmonitor-valid", "main.txt")
        self.assertEqual(self.evaluate(head)["status"], "unchanged")
        self.at(leaf, "update-index", "--fsmonitor-valid", "main.txt")
        (leaf / "main.txt").write_text("hidden by nested monitor")
        self.assertEqual(self.at(leaf, "status", "--porcelain=v1"), "")
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        before = self.metadata(repos)
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertEqual(self.metadata(repos), before)

    def test_actual_gitlink_index_paths_are_used_without_shell_expansion(self):
        name = "lib ; $(touch SOURCE_SHELL_MARKER) [*]"
        repos = self.modules(name=name)
        # A missing .gitmodules mapping must not hide the actual cached gitlink.
        self.git("rm", "--", ".gitmodules")
        self.git("commit", "-qm", "retain indexed module without mapping")
        head, pinned = self.versions()
        self.assertEqual(self.evaluate(head)["status"], "unchanged")
        self.at(repos[-1], "update-index", "--assume-unchanged", "main.txt")
        (repos[-1] / "main.txt").write_text("hidden actual index path")
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertFalse((self.repo / "SOURCE_SHELL_MARKER").exists())
        self.assertFalse((self.root / "SOURCE_SHELL_MARKER").exists())

    def test_missing_or_uninitialized_module_is_unknown_without_initialization(self):
        repos = self.modules(nested=True)
        head, pinned = self.versions()
        leaf = repos[-1]
        shutil.rmtree(leaf)
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.assertFalse(leaf.exists())
        leaf.mkdir()
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.assertFalse((leaf / ".git").exists())
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")

    def test_one_reader_budget_covers_nested_worktrees_and_pinned_read_remains_one_call(self):
        repos = self.modules(nested=True)
        head, pinned = self.versions()
        before = self.metadata(repos)
        reader = sources.Reader()
        with patch.object(sources, "MAX_CALLS", 12), patch.object(sources, "Reader", return_value=reader) as factory:
            result = self.evaluate(head)
        factory.assert_called_once()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("budget", result["reason"])
        self.assertLessEqual(reader.calls, 13)
        with patch.object(sources, "MAX_CALLS", 1):
            self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertEqual(self.metadata(repos), before)

    def test_module_commit_changes_and_staged_gitlink_changes_block_head(self):
        repos = self.modules()
        module = repos[-1]
        head, pinned = self.versions()
        (module / "main.txt").write_text("v2")
        self.at(module, "add", "main.txt")
        self.at(module, "-c", "user.name=Synthetic submodule", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "advance module")
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.git("add", "library")
        self.assertEqual(self.git("status", "--porcelain=v1", "--ignore-submodules=all"), "")
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.git("commit", "-qm", "record module advance in parent")
        result = self.evaluate(head)
        self.assertEqual(result["status"], "changed")
        expected = sources.digest({"kind": "git", "commit": self.git("rev-parse", "HEAD")})
        self.assertEqual(result["observed"], expected)
        self.assertEqual(result["expected"], head["fingerprint"])
        fresh = sources.capture(self.git_spec, self.root)
        self.assertEqual(fresh["fingerprint"], expected)
        self.assertEqual(self.evaluate(fresh)["status"], "unchanged")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")

    def test_staged_gitlink_change_inside_nested_module_is_unknown(self):
        repos = self.modules(nested=True)
        parent, leaf = repos[-2:]
        head, pinned = self.versions()
        (leaf / "main.txt").write_text("v2")
        self.at(leaf, "add", "main.txt")
        self.at(leaf, "-c", "user.name=Synthetic submodule", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "advance nested module")
        self.at(parent, "add", "nested leaf")
        self.assertNotEqual(self.at(parent, "status", "--porcelain=v1"), "")
        self.assertEqual(self.at(parent, "status", "--porcelain=v1", "--ignore-submodules=all"), "")
        before = self.metadata(repos)
        self.assertEqual(self.evaluate(head)["status"], "unknown")
        self.assertEqual(self.evaluate(pinned)["status"], "unchanged")
        self.assertEqual(self.metadata(repos), before)


class GithubTests(SourcesFixture):
    def setUp(self):
        super().setUp()
        self.issue_spec = {"id": "issue", "role": "ticket", "kind": "github_issue", "repository": "Owner/Repo", "number": 7}
        self.check_spec = {"id": "checks", "role": "ci", "kind": "github_checks", "repository": "Owner/Repo", "ref": "main"}

    def capture_remote(self, spec, table):
        stub = GithubStub(table)
        with patch.object(sources.subprocess, "run", stub):
            captured = sources.capture(spec, self.root)
        return captured, stub

    def observe(self, spec, table):
        stub = GithubStub(table)
        with patch.object(sources.subprocess, "run", stub):
            result = self.evaluate(spec)
        return result, stub

    def test_issue_requirements_and_metadata_are_fresh_content_not_ttl(self):
        table = {"repos/owner/repo/issues/7": issue()}
        captured, _ = self.capture_remote(self.issue_spec, table)
        captured["observed_at"] = "1900-01-01T00:00:00Z"
        result, _ = self.observe(captured, table)
        self.assertEqual(result["status"], "unchanged")
        for changes in ({"title": "New scope"}, {"body": "New criteria"}, {"state": "closed"},
                        {"updated_at": "2026-10-24T13:00:00Z"}, {"labels": []}, {"milestone": None}):
            with self.subTest(changes=changes):
                altered = dict(issue(), **changes)
                result, _ = self.observe(captured, {"repos/owner/repo/issues/7": altered})
                self.assertEqual(result["status"], "changed")
        altered = issue()
        altered["milestone"]["description"] = "Different release scope"
        self.assertEqual(self.observe(captured, {"repos/owner/repo/issues/7": altered})[0]["status"], "changed")

    def test_issue_label_order_and_unrelated_counters_do_not_change_digest(self):
        table = {"repos/owner/repo/issues/7": issue()}
        captured, _ = self.capture_remote(self.issue_spec, table)
        table["repos/owner/repo/issues/7"]["labels"].reverse()
        table["repos/owner/repo/issues/7"]["comments"] = 23
        self.assertEqual(self.observe(captured, table)[0]["status"], "unchanged")

    def test_missing_partial_redacted_and_error_issue_responses_are_unknown(self):
        captured, _ = self.capture_remote(self.issue_spec, {"repos/owner/repo/issues/7": issue()})
        incomplete = issue()
        del incomplete["body"]
        malformed = issue()
        malformed["labels"] = ["required"]
        for value in ({"message": "private-token"}, incomplete, malformed, dict(issue(), number=8),
                      dict(issue(), body="[REDACTED]"), None):
            with self.subTest(value=value):
                result, _ = self.observe(captured, {"repos/owner/repo/issues/7": value})
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn("private-token", json.dumps(result))

    def test_ci_observation_is_bound_to_sha_and_retains_failed_states(self):
        table = checks_table()
        captured, stub = self.capture_remote(self.check_spec, table)
        self.assertEqual(stub.calls.count("repos/owner/repo/commits/main"), 2)
        self.assertTrue(any(SHA + "/statuses" in call for call in stub.calls))
        self.assertTrue(any("filter=all" in call for call in stub.calls))
        result, _ = self.observe(captured, table)
        self.assertEqual(result["status"], "unchanged")
        table["repos/owner/repo/check-suites/1/check-runs?filter=all&per_page=100&page=1"]["check_runs"][0]["conclusion"] = "failure"
        changed, _ = self.observe(captured, table)
        self.assertEqual(changed["status"], "changed")
        failed, _ = self.capture_remote(self.check_spec, table)
        self.assertEqual(self.observe(failed, table)[0]["status"], "unchanged")
        self.assertNotIn("passed", json.dumps(self.observe(failed, table)[0]))

    def test_ci_head_attempt_status_and_timestamp_changes_invalidate(self):
        original = checks_table()
        captured, _ = self.capture_remote(self.check_spec, original)
        run_key = "repos/owner/repo/check-suites/1/check-runs?filter=all&per_page=100&page=1"
        for changes in ({"id": 12}, {"external_id": "attempt-2"}, {"run_attempt": 2},
                        {"details_url": "https://example.invalid/run/1/attempts/2"},
                        {"started_at": "2026-10-24T13:00:00Z"}, {"updated_at": "2026-10-24T13:00:00Z"}):
            with self.subTest(changes=changes):
                table = copy.deepcopy(original)
                table[run_key]["check_runs"][0].update(changes)
                self.assertEqual(self.observe(captured, table)[0]["status"], "changed")
        table = copy.deepcopy(original)
        table[f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1"][0]["state"] = "pending"
        self.assertEqual(self.observe(captured, table)[0]["status"], "changed")
        table = {key.replace(SHA, OTHER_SHA): value for key, value in copy.deepcopy(original).items()}
        table["repos/owner/repo/commits/main"] = {"sha": OTHER_SHA}
        for value in table.values():
            if isinstance(value, dict):
                for row in value.get("check_suites", []) + value.get("check_runs", []):
                    row["head_sha"] = OTHER_SHA
        self.assertEqual(self.observe(captured, table)[0]["status"], "changed")

    def test_ci_paginated_suites_runs_and_statuses_all_affect_digest(self):
        table = checks_table()
        suite_key = f"repos/owner/repo/commits/{SHA}/check-suites?per_page=100&page=1"
        table[suite_key] = {"total_count": 101, "check_suites": [suite(i) for i in range(1, 101)]}
        table[suite_key.replace("&page=1", "&page=2")] = {"total_count": 101, "check_suites": [suite(101)]}
        for i in range(1, 102):
            table[f"repos/owner/repo/check-suites/{i}/check-runs?filter=all&per_page=100&page=1"] = {"total_count": 0, "check_runs": []}
        run_key = "repos/owner/repo/check-suites/101/check-runs?filter=all&per_page=100&page=1"
        table[run_key] = {"total_count": 101, "check_runs": [run(i, 101) for i in range(1, 101)]}
        table[run_key.replace("&page=1", "&page=2")] = {"total_count": 101, "check_runs": [run(101, 101)]}
        status_key = f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1"
        table[status_key] = [status(i) for i in range(1, 101)]
        table[status_key.replace("&page=1", "&page=2")] = [status(101)]
        captured, stub = self.capture_remote(self.check_spec, table)
        self.assertIn(run_key.replace("&page=1", "&page=2"), stub.calls)
        self.assertIn(status_key.replace("&page=1", "&page=2"), stub.calls)
        table[status_key.replace("&page=1", "&page=2")][0]["state"] = "failure"
        self.assertEqual(self.observe(captured, table)[0]["status"], "changed")
        table[run_key.replace("&page=1", "&page=2")]["check_runs"][0]["run_attempt"] = 2
        new, _ = self.capture_remote(self.check_spec, table)
        self.assertNotEqual(new["fingerprint"], captured["fingerprint"])

    def test_ci_incomplete_duplicate_wrong_sha_and_wrong_suite_are_unknown(self):
        original = checks_table()
        captured, _ = self.capture_remote(self.check_spec, original)
        suite_key = f"repos/owner/repo/commits/{SHA}/check-suites?per_page=100&page=1"
        run_key = "repos/owner/repo/check-suites/1/check-runs?filter=all&per_page=100&page=1"
        cases = [(suite_key, {"total_count": 2, "check_suites": [suite()]}),
                 (suite_key, {"check_suites": [suite()]}),
                 (suite_key, {"total_count": 2, "check_suites": [suite(), suite()]}),
                 (suite_key, {"total_count": True, "check_suites": [suite()]}),
                 (run_key, {"total_count": 1, "check_runs": [dict(run(), head_sha=OTHER_SHA)]}),
                 (run_key, {"total_count": 1, "check_runs": [run(suite_id=99)]}),
                 (run_key, {"total_count": 1, "check_runs": [dict(run(), status="unknown-status")]}),
                 (run_key, {"total_count": 1, "check_runs": [dict(run(), started_at="invalid")]}),
                 (run_key, {"total_count": 1, "check_runs": [dict(run(), run_attempt=True)]})]
        for key, value in cases:
            with self.subTest(value=value):
                table = copy.deepcopy(original)
                table[key] = value
                self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")

    def test_pagination_changing_totals_and_duplicate_statuses_are_unknown(self):
        original = checks_table()
        captured, _ = self.capture_remote(self.check_spec, original)
        table = copy.deepcopy(original)
        key = f"repos/owner/repo/commits/{SHA}/check-suites?per_page=100&page=1"
        table[key] = {"total_count": 101, "check_suites": [suite(i) for i in range(1, 101)]}
        table[key.replace("&page=1", "&page=2")] = {"total_count": 102, "check_suites": [suite(101)]}
        self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")
        table = copy.deepcopy(original)
        key = f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1"
        table[key] = [status(i) for i in range(1, 101)]
        table[key.replace("&page=1", "&page=2")] = [status(1)]
        self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")
        with patch.object(sources, "MAX_PAGES", 1):
            self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")

    def test_ref_moving_during_ci_observation_is_unknown(self):
        table = checks_table()
        captured, _ = self.capture_remote(self.check_spec, table)
        values = iter([{"sha": SHA}, {"sha": OTHER_SHA}])
        table["repos/owner/repo/commits/main"] = lambda: next(values)
        self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")

    def test_empty_observation_is_version_equality_not_ci_success(self):
        table = checks_table()
        table[f"repos/owner/repo/commits/{SHA}/check-suites?per_page=100&page=1"] = {"total_count": 0, "check_suites": []}
        table[f"repos/owner/repo/commits/{SHA}/statuses?per_page=100&page=1"] = []
        captured, _ = self.capture_remote(self.check_spec, table)
        self.assertEqual(self.observe(captured, table)[0]["status"], "unchanged")

    def test_host_and_ref_cannot_inject_commands_or_other_endpoints(self):
        with patch.object(sources.subprocess, "run", side_effect=AssertionError("must not call")):
            for repository in ("https://private-token@github.com/owner/repo", "owner/repo?secret=x", "owner/../repo", "owner/.."):
                with self.subTest(repository=repository), self.assertRaises(ValueError):
                    sources.capture(dict(self.issue_spec, repository=repository), self.root)
        ref = "heads/feature;$(echo private-token)?x={owner}"
        table = checks_table()
        endpoint = "repos/owner/repo/commits/heads%2Ffeature%3B%24%28echo%20private-token%29%3Fx%3D%7Bowner%7D"
        table[endpoint] = table.pop("repos/owner/repo/commits/main")
        captured, stub = self.capture_remote(dict(self.check_spec, ref=ref), table)
        self.assertIn(endpoint, stub.calls)
        self.assertRegex(captured["fingerprint"], r"^[0-9a-f]{64}$")

    def test_process_errors_diagnostics_redaction_and_malformed_json_are_sanitized(self):
        captured = dict(self.issue_spec, fingerprint="a" * 64)
        endpoint = "repos/owner/repo/issues/7"
        failures = [FileNotFoundError("private-token"), subprocess.TimeoutExpired("private-token", 10),
                    subprocess.CompletedProcess([], 1, b"private-token", b"Authorization: private-token"),
                    subprocess.CompletedProcess([], 0, json.dumps(issue()).encode(), b"private-token"),
                    subprocess.CompletedProcess([], 0, b"{private-token}", b""),
                    subprocess.CompletedProcess([], 0, b"\xffprivate-token", b""),
                    subprocess.CompletedProcess([], 0, b"[REDACTED]", b"")]
        for value in failures:
            with self.subTest(value=type(value).__name__):
                result, _ = self.observe(captured, {endpoint: value})
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["observed"])
                self.assertNotIn("private-token", json.dumps(result))
        with patch.object(sources, "MAX_BYTES", 1):
            self.assertEqual(self.observe(captured, {endpoint: issue()})[0]["status"], "unknown")

    def test_call_and_pagination_limits_fail_closed_and_disable_inherited_debug(self):
        table = checks_table()
        captured, _ = self.capture_remote(self.check_spec, table)
        with patch.object(sources, "MAX_CALLS", 1):
            self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")
        with patch.object(sources, "MAX_PAGES", 0):
            self.assertEqual(self.observe(captured, table)[0]["status"], "unknown")
        with patch.dict(os.environ, {"GH_DEBUG": "api", "GIT_TRACE": "1"}):
            self.assertEqual(self.observe(captured, table)[0]["status"], "unchanged")
        with patch.object(sources.time, "monotonic", side_effect=[1, 100]):
            result, stub = self.observe(dict(self.issue_spec, fingerprint="a" * 64), {"repos/owner/repo/issues/7": issue()})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
