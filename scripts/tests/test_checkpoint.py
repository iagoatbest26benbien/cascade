"""Transport tests use temporary LOCAL bare Git remotes only, never a service."""

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


SCRIPT = Path(__file__).resolve().parents[1] / "checkpoint.py"
spec = importlib.util.spec_from_file_location("checkpoint", SCRIPT)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)
TEMPLATE = (SCRIPT.parent.parent / "assets/tracker.html").read_text(encoding="utf-8")


def native_git(*args):
    result = subprocess.run(["git", *map(str, args)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode().strip()


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="checkpoint-tests-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "original workstream"
        self.root.mkdir()
        self.remote = self.base / "local remote.git"
        native_git("init", "--bare", "--quiet", self.remote)
        self.tracker, _ = cp.tracker_module()
        self.state = {
            "title": "Recovery fixture", "spec": "https://example.test/issues/123", "scope": "one mission",
            "updated_at": "2026-10-23T10:00:00+00:00", "next_action": "Reconcile unknown delivery before retrying.",
            "decisions": ["Retry limit 2; escalate to Marie."], "journal": [f"Previous report: {self.root}/evidence/M-1/report.md"],
            "missions": [{"id": "M-1", "title": "Recover", "ticket": "https://example.test/issues/123",
                          "status": "to-verify", "owner": "Marie", "chat": "codex://threads/reference",
                          "workspace": str(self.root), "criteria": [{"text": "Exact evidence", "result": "unverified"}],
                          "report": str(self.root / "evidence/M-1/report.md"), "depends_on": [], "remaining": ["Receive"],
                          "evidence": [str(self.root / "evidence/M-1/report.md")],
                          "attempts": {"finding-1": 2}, "dispatch": {"status": "unknown", "request": "request-7"},
                          "prompt": {"kind": "follow-up", "delivery": "unknown", "file": "prompts/M-1-follow-up.md",
                                     "text": f"$implement https://example.test/issues/123\nRead {self.root}/tracker.html.\n"
                                             f"Save report at {self.root}/evidence/M-1/new-report.md.\n"}}],
        }
        self.write_state()
        (self.root / "evidence/M-1").mkdir(parents=True)
        (self.root / "evidence/M-1/report.md").write_bytes(b"Historical bytes: \xff\r\n\x00\n")
        (self.root / "prompts/historical.md").write_text(f"Original: {self.root}/tracker.html\n", encoding="utf-8")
        (self.root / "context.md").write_text(f"Read {self.root}/tracker.html\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def write_state(self):
        (self.root / "tracker.html").write_text(self.tracker.render(TEMPLATE, self.state), encoding="utf-8")
        for mission in self.state["missions"]:
            path = self.root / mission["prompt"]["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mission["prompt"]["text"].rstrip() + "\n", encoding="utf-8")

    def snapshot(self, includes=()):
        return cp.make_snapshot(self.root, "tracker.html", includes)

    def publish(self, expected="absent", includes=()):
        return cp.publish(self.root, "tracker.html", includes, str(self.remote), "recovery/main", expected)

    def resign(self, files, manifest=None):
        manifest = json.loads(files[cp.MANIFEST]) if manifest is None else manifest
        manifest["files"] = {p: cp.digest(v) for p, v in files.items() if p != cp.MANIFEST}
        files[cp.MANIFEST] = cp.json_bytes(manifest)
        return files

    def test_round_trip_remote_fresh_clone_and_portable_cli_after_source_loss(self):
        before = cp.files_under(self.root)
        publication = self.publish()
        self.assertTrue(publication["remote_confirmed"])
        self.assertFalse(publication["disaster_recovery_tested"])
        self.assertEqual(before, cp.files_under(self.root))
        first = publication["commit"]
        self.state["next_action"] = "Reread the latest receipt."
        self.write_state()
        second = self.publish(first)["commit"]
        self.assertEqual(native_git("--git-dir", self.remote, "rev-parse", second + "^"), first)
        clone = self.base / "fresh clone"
        native_git("clone", "--quiet", "--branch", "recovery/main", self.remote, clone)
        stored = cp.local_snapshot(clone)
        manifest = cp.verify_files(stored)
        self.assertEqual(manifest["original_root"], str(self.root))
        self.assertNotIn(str(self.remote), json.dumps(manifest))
        exact = {k: v for k, v in stored.items() if k.startswith("source/")}
        shutil.rmtree(self.root)
        destination = self.base / "restored workstream"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(clone / "tools/checkpoint.py"), "restore",
                                 "--snapshot", str(clone), "--destination", str(destination)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["active_schema_and_exports_checked"])
        original_state = cp.state_from(exact["source/tracker.html"], self.tracker)[1]
        active_state = cp.state_from((destination / "tracker.html").read_bytes(), self.tracker)[1]
        for field in ("owner", "attempts", "dispatch"):
            self.assertEqual(active_state["missions"][0][field], original_state["missions"][0][field])
        self.assertEqual(active_state["missions"][0]["prompt"]["delivery"], "unknown")
        self.assertNotIn(str(self.root), (destination / "tracker.html").read_text())
        self.assertIn(str(destination), (destination / "prompts/historical.md").read_text())
        self.assertEqual((destination / "evidence/M-1/report.md").read_bytes(), b"Historical bytes: \xff\r\n\x00\n")
        for path, content in stored.items():
            self.assertEqual((destination / ".checkpoint" / path).read_bytes(), content)
        check = subprocess.run([sys.executable, "-B", str(clone / "tools/tracker.py"), "check", str(destination / "tracker.html")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(check.returncode, 0, check.stderr.decode())
        cp.verify_files(cp.local_snapshot(destination / ".checkpoint"))

    def test_stale_and_absent_bases_do_not_change_remote(self):
        first = self.publish()["commit"]
        for expected in ("absent", "0" * 40):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "Stale base"):
                self.publish(expected)
        self.assertEqual(native_git("--git-dir", self.remote, "rev-parse", "refs/heads/recovery/main"), first)

    def test_successful_push_with_failed_remote_read_does_not_claim_confirmation(self):
        with patch.object(cp, "remote_snapshot", side_effect=ValueError("read interrupted")):
            with self.assertRaisesRegex(ValueError, "Push accepted .*fresh remote confirmation failed.*Do not retry blindly"):
                self.publish()
        actual = native_git("--git-dir", self.remote, "rev-parse", "refs/heads/recovery/main")
        self.assertEqual(len(actual), 40)
        with self.assertRaisesRegex(ValueError, "Stale base"):
            self.publish()

    def test_snapshot_can_be_published_again_from_a_restored_workstream(self):
        files = self.snapshot()
        destination = self.base / "first restore"
        cp.restore(files, destination)
        result = cp.publish(destination, "tracker.html", [], str(self.remote), "recovery/main", "absent")
        _, remote_files = cp.remote_snapshot(str(self.remote), "recovery/main", result["commit"])
        self.assertNotIn("source/.checkpoint/manifest.json", remote_files)
        second_destination = self.base / "second restore"
        cp.restore(remote_files, second_destination)
        state = cp.state_from((second_destination / "tracker.html").read_bytes(), self.tracker)[1]
        self.assertEqual(state["missions"][0]["workspace"], str(second_destination))

    def test_original_request_payload_survives_two_republications(self):
        original = (self.root / "prompts/M-1-follow-up.md").read_bytes()
        self.state["missions"][0]["dispatch"]["payload_sha256"] = cp.digest(original)
        self.write_state()
        files = self.snapshot()
        for i in range(2):
            target = self.base / f"publication-{i}"
            cp.restore(files, target)
            files = cp.make_snapshot(target, "tracker.html", [])
        manifest = cp.verify_files(files)
        archive = "originals/" + cp.digest(original) + ".prompt"
        self.assertEqual(files[archive], original)
        self.assertTrue(any(p["sha256"] == cp.digest(original)
                            and p["mission"] == "M-1"
                            and p["dispatch"]["request"] == "request-7"
                            for p in manifest["payloads"]))

    def test_missing_factual_evidence_blocks_publication(self):
        missing = self.root / "evidence/M-1/acceptance.bin"
        mission = self.state["missions"][0]
        mission.update(status="done", remaining=[])
        mission["criteria"] = [{"text": "Accepted", "result": "passed", "proof": str(missing)}]
        mission["evidence"].append(str(missing))
        self.write_state()
        with self.assertRaisesRegex(ValueError, "not captured"):
            self.publish()
        self.assertEqual(native_git("--git-dir", self.remote, "for-each-ref", "--format=%(objectname)"), "")

    def test_older_checkpoint_without_payload_archive_upgrades_losslessly(self):
        files = self.snapshot()
        manifest = json.loads(files[cp.MANIFEST])
        original = files["source/prompts/M-1-follow-up.md"]
        files = {k: v for k, v in files.items() if not k.startswith("originals/")}
        del manifest["payloads"]
        self.resign(files, manifest)
        target = self.base / "legacy-restore"
        cp.restore(files, target)
        upgraded = cp.make_snapshot(target, "tracker.html", [])
        self.assertEqual(upgraded["originals/" + cp.digest(original) + ".prompt"], original)
        cp.verify_files(upgraded)

    def test_relative_factual_evidence_blocks_publication(self):
        mission = self.state["missions"][0]
        mission.update(status="done", remaining=[])
        for proof in ("evidence/M-1/missing.bin", "[acceptance](evidence/M-1/missing.bin)"):
            mission["criteria"] = [{"text": "Accepted", "result": "passed", "proof": proof}]
            mission["evidence"] = [proof]
            self.write_state()
            with self.subTest(proof=proof), self.assertRaisesRegex(ValueError, "missing local Markdown dependency"):
                self.publish()

    def test_legacy_request_without_digest_keeps_one_original_identity(self):
        original = (self.root / "prompts/M-1-follow-up.md").read_bytes()
        files = self.snapshot()
        for i in range(3):
            target = self.base / f"no-digest-{i}"
            cp.restore(files, target)
            files = cp.make_snapshot(target, "tracker.html", [])
        manifest = cp.verify_files(files)
        associated = [p for p in manifest["payloads"] if p.get("dispatch", {}).get("request") == "request-7"]
        self.assertEqual({p["sha256"] for p in associated}, {cp.digest(original)})
        self.assertTrue(any(p.get("view_of_payload") == cp.digest(original) for p in manifest["payloads"]))

    def test_factual_markdown_titles_are_not_part_of_dependency_paths(self):
        mission = self.state["missions"][0]
        mission.update(status="done", remaining=[])
        for target in ("evidence/M-1/report.md", "<evidence/M-1/report.md>", str(self.root / "evidence/M-1/report.md")):
            for title in ('"acceptance receipt"', "'acceptance receipt'", '(acceptance receipt)', '"/not/a/dependency evidence/not-real.bin"'):
                proof = f'[acceptance]({target} {title})'
                mission["criteria"] = [{"text": "Accepted", "result": "passed", "proof": proof}]
                mission["evidence"] = [proof]
                self.write_state()
                with self.subTest(proof=proof):
                    files = self.snapshot()
                    cp.verify_files(files)

    def test_file_urls_and_nested_destinations_are_relocated_once(self):
        self.state["missions"][0]["prompt"]["text"] += f"Read {self.root.as_uri()}/tracker.html#M-1\n"
        self.write_state()
        files = self.snapshot()
        destination = self.root / "restored inside"
        cp.restore(files, destination)
        prompt = (destination / "prompts/M-1-follow-up.md").read_text()
        self.assertIn(destination.as_uri() + "/tracker.html#M-1", prompt)
        self.assertNotIn("restored inside/restored inside", prompt)

    def test_nested_tracker_relative_path_is_retained(self):
        nested = self.root / "nested"
        nested.mkdir()
        for name in ("tracker.html", "prompts", "evidence", "context.md"):
            shutil.move(self.root / name, nested / name)
        # Recreate paths in the source state/export for the nested layout.
        state_path = nested / "tracker.html"
        content = state_path.read_text().replace(str(self.root), str(nested))
        state_path.write_text(content, encoding="utf-8")
        for name in ("M-1-follow-up.md", "historical.md"):
            path = nested / "prompts" / name
            path.write_text(path.read_text().replace(str(self.root), str(nested)), encoding="utf-8")
        context = nested / "context.md"
        context.write_text(context.read_text().replace(str(self.root), str(nested)), encoding="utf-8")
        files = cp.make_snapshot(self.root, "nested/tracker.html", [])
        destination = self.base / "nested restore"
        receipt = cp.restore(files, destination)
        self.assertEqual(receipt["tracker"], str(destination / "nested/tracker.html"))

    def test_remote_symlink_mode_is_rejected_without_checkout(self):
        # Build an intentionally invalid tree in a temporary local fixture repo.
        repo = self.base / "malformed fixture"
        cp.init_repo(repo)
        oid = cp.git(repo, "hash-object", "-w", "--stdin", input_data=b"/outside").strip()
        cp.git(repo, "update-index", "-z", "--index-info", input_data=b"120000 " + oid + b"\tlink\0")
        tree = cp.git(repo, "write-tree").decode().strip()
        commit = cp.git(repo, "commit-tree", tree, input_data=b"symlink fixture\n").decode().strip()
        cp.git(repo, "push", str(self.remote), commit + ":refs/heads/malformed")
        with self.assertRaisesRegex(ValueError, "symlink"):
            cp.remote_snapshot(str(self.remote), "malformed")

    def test_concurrent_snapshot_is_rejected_by_non_force_push(self):
        first = self.publish()["commit"]
        original_git = cp.git
        raced = False
        concurrent = None
        def racing_git(directory, *args, **kwargs):
            nonlocal raced, concurrent
            if args[0] == "push" and not raced:
                raced = True
                self.state["next_action"] = "Concurrent snapshot"
                self.write_state()
                with patch.object(cp, "git", original_git):
                    concurrent = self.publish(first)["commit"]
            return original_git(directory, *args, **kwargs)
        with patch.object(cp, "git", racing_git), self.assertRaisesRegex(ValueError, "git push failed"):
            self.publish(first)
        self.assertEqual(native_git("--git-dir", self.remote, "rev-parse", "refs/heads/recovery/main"), concurrent)

    def test_missing_external_dependency_blocks_until_explicit_file_and_directory_includes(self):
        implement = self.base / "implement skill"
        (implement / "references").mkdir(parents=True)
        (implement / "SKILL.md").write_text("Read [rules](references/rules.md).\n", encoding="utf-8")
        (implement / "references/rules.md").write_text("Stay scoped.\n", encoding="utf-8")
        fixture = self.base / "fixture data.json"
        fixture.write_bytes(b'{"input":42}\r\n')
        self.state["missions"][0]["prompt"]["text"] += f"Read [implement]({implement}/SKILL.md) and `{fixture}`.\n"
        self.write_state()
        with self.assertRaisesRegex(ValueError, "--include"):
            self.snapshot()
        files = self.snapshot([f"{implement}=dependencies/implement", f"{fixture}=dependencies/fixture.json"])
        destination = self.base / "included restore"
        cp.restore(files, destination)
        prompt = (destination / "prompts/M-1-follow-up.md").read_text()
        self.assertIn(str(destination / "dependencies/implement/SKILL.md"), prompt)
        self.assertIn(str(destination / "dependencies/fixture.json"), prompt)
        self.assertEqual((destination / "dependencies/fixture.json").read_bytes(), fixture.read_bytes())
        self.assertEqual((destination / ".checkpoint/source/prompts/M-1-follow-up.md").read_bytes(),
                         (self.root / "prompts/M-1-follow-up.md").read_bytes())

    def test_relative_skill_dependency_must_also_be_captured(self):
        implement = self.base / "SKILL.md"
        implement.write_text("Read [missing](references/missing.md).\n", encoding="utf-8")
        self.state["missions"][0]["prompt"]["text"] += f"Read `{implement}`.\n"
        self.write_state()
        with self.assertRaisesRegex(ValueError, "missing local Markdown dependency"):
            self.snapshot([f"{implement}=dependencies/SKILL.md"])

    def test_changed_missing_extra_and_symlink_snapshot_files_are_rejected_before_destination(self):
        original = self.snapshot()
        for kind in ("changed", "missing", "extra"):
            files = dict(original)
            if kind == "changed":
                files["source/prompts/historical.md"] += b"tamper"
            elif kind == "missing":
                del files["source/prompts/historical.md"]
            else:
                files["source/extra.md"] = b"extra"
            destination = self.base / kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                cp.restore(files, destination)
            self.assertFalse(destination.exists())
        clone = self.base / "snapshot"
        cp.write_files(clone, original)
        target = clone / "source/prompts/historical.md"
        target.unlink()
        target.symlink_to(self.root / "prompts/historical.md")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            cp.local_snapshot(clone)

    def test_unsafe_manifest_paths_schemas_mappings_and_collisions_are_rejected(self):
        for path in ("source/../escape", "/absolute", "source/.git/config", "source/.checkpoint/file", "source/a\\b", "source//a"):
            files = self.snapshot()
            files[path] = b"bad"
            self.resign(files)
            with self.subTest(path=path), self.assertRaises(ValueError):
                cp.restore(files, self.base / "no-output")
            self.assertFalse((self.base / "no-output").exists())
        files = self.snapshot()
        files["source/prompts"] = b"file conflicts with directory"
        self.resign(files)
        with self.assertRaisesRegex(ValueError, "collision"):
            cp.verify_files(files)
        for field in ("schema", "tracker_schema"):
            files = self.snapshot()
            manifest = json.loads(files[cp.MANIFEST])
            manifest[field] = "future/v99"
            files[cp.MANIFEST] = cp.json_bytes(manifest)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Unsupported"):
                cp.verify_files(files)

    def test_new_destination_only_and_no_symlink_ancestors(self):
        files = self.snapshot()
        with self.assertRaisesRegex(ValueError, "NEW destination"):
            cp.restore(files, self.root)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            cp.restore(files, alias / "child")
        (self.root / "prompts/link.md").symlink_to(self.root / "tracker.html")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.snapshot()

    def test_ref_deletion_or_rewind_between_lookup_and_push_is_rejected(self):
        first = self.publish()["commit"]
        self.state["next_action"] = "second checkpoint"
        self.write_state()
        second = self.publish(first)["commit"]
        actual_git = cp.git
        for new_value in (None, first):
            native_git("--git-dir", self.remote, "update-ref", "refs/heads/recovery/main", second)
            def change_ref(directory, *args, input_data=None):
                if args[0] == "push":
                    if new_value is None:
                        native_git("--git-dir", self.remote, "update-ref", "-d", "refs/heads/recovery/main")
                    else:
                        native_git("--git-dir", self.remote, "update-ref", "refs/heads/recovery/main", new_value)
                return actual_git(directory, *args, input_data=input_data)
            with self.subTest(new_value=new_value), patch.object(cp, "git", side_effect=change_ref):
                with self.assertRaisesRegex(ValueError, "Push rejected or outcome uncertain"):
                    self.publish(second)
            observed = native_git("--git-dir", self.remote, "for-each-ref", "--format=%(objectname)", "refs/heads/recovery/main")
            self.assertEqual(observed, new_value or "")

    def put_catalog(self, entries):
        repo = self.base / "catalog-repo"
        cp.init_repo(repo)
        cp.write_files(repo, {"registry.json": cp.json_bytes({"schema": cp.CATALOG_SCHEMA, "workstreams": entries}),
                              "README.md": b"Shared directory; snapshots use recovery branches.\n"})
        cp.git(repo, "add", "--all")
        cp.git(repo, "commit", "--quiet", "-m", "Shared catalog")
        cp.git(repo, "push", "--quiet", str(self.remote), "HEAD:refs/heads/main")
        return cp.git(repo, "rev-parse", "HEAD").decode().strip()

    def catalog_entry(self, project="project-a", branch="recovery/main"):
        return {"id": project + "/delivery", "project": project, "title": "Delivery",
                "source": "https://example.test/" + project, "branch": branch,
                "owner": "coordinator", "status": "active"}

    def test_shared_catalog_resolves_live_heads_and_unregistered_branches(self):
        checkpoint = self.publish()["commit"]
        native_git("--git-dir", self.remote, "update-ref", "refs/heads/recovery/unregistered", checkpoint)
        catalog_sha = self.put_catalog([self.catalog_entry(),
                                       self.catalog_entry("project-b", "recovery/project-b/delivery")])
        result = cp.catalog(str(self.remote))
        self.assertEqual(result["catalog_commit"], catalog_sha)
        self.assertEqual(result["workstreams"][0]["checkpoint_commit"], checkpoint)
        self.assertFalse(result["workstreams"][1]["checkpoint_available"])
        self.assertEqual(result["unregistered_branches"], ["recovery/unregistered"])
        self.assertEqual(cp.catalog(str(self.remote), "project-a/delivery")["workstreams"][0]["branch"], "recovery/main")
        with self.assertRaisesRegex(ValueError, "Unknown workstream"):
            cp.catalog(str(self.remote), "not-a-project/new")

    def test_checkpoint_cannot_replace_shared_catalog(self):
        catalog_sha = self.put_catalog([self.catalog_entry()])
        with self.assertRaisesRegex(ValueError, "Refusing to overwrite the shared catalog"):
            cp.publish(self.root, "tracker.html", [], str(self.remote), "main", catalog_sha)
        self.assertEqual(native_git("--git-dir", self.remote, "rev-parse", "refs/heads/main"), catalog_sha)

    def test_shared_catalog_rejects_ambiguous_id_branch_or_source(self):
        first = self.catalog_entry()
        second = self.catalog_entry("project-b", "recovery/project-b/delivery")
        for field in ("id", "branch", "source"):
            duplicate = dict(second, **{field: first[field]})
            if field == "id":
                duplicate["project"] = first["project"]
            # The catalog itself is the input; use a separate local repo per case.
            repo = self.base / "catalog-repo"
            if repo.exists():
                shutil.rmtree(repo)
            native_git("--git-dir", self.remote, "update-ref", "-d", "refs/heads/main")
            self.put_catalog([first, duplicate])
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Duplicate catalog"):
                cp.catalog(str(self.remote))

    def test_credentials_and_unsupported_remotes_rejected_without_echoing_value(self):
        for remote in ("https://token@example.test/repo.git", "https://user:password@example.test/repo.git",
                       "https://example.test/repo?token=secret", "https://example.test/repo#secret",
                       "ssh://git:secret@example.test/repo", "ext::sh command", "relative/path",
                       "file:///tmp/remote", "ssh://secret@example.test/repo"):
            with self.subTest(remote=remote), self.assertRaises(ValueError) as caught:
                cp.safe_remote(remote)
            self.assertNotIn(remote, str(caught.exception))
        for remote in (str(self.remote), "https://example.test/repo.git", "git@example.test:org/repo.git", "ssh://git@example.test/org/repo.git"):
            cp.safe_remote(remote)

    def test_git_ignores_attributes_global_config_and_caller_repository(self):
        (self.root / "dependencies").mkdir()
        (self.root / "dependencies/.gitattributes").write_text("*.bin text eol=lf\n", encoding="utf-8")
        (self.root / "dependencies/.gitignore").write_text("*.bin\n", encoding="utf-8")
        content = b"exact\r\nbytes\r\n"
        (self.root / "dependencies/input.bin").write_bytes(content)
        config = self.base / "caller-config"
        config.write_text("[user]\n name = Private user\n[init]\n templateDir = /does-not-exist\n", encoding="utf-8")
        caller = self.base / "caller"
        caller.mkdir()
        native_git("-C", caller, "init", "--quiet")
        before = cp.files_under(caller / ".git")
        config_bytes = config.read_bytes()
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_DIR": str(caller / ".git")}):
            publication = self.publish()
        _, files = cp.remote_snapshot(str(self.remote), "recovery/main", publication["commit"])
        self.assertEqual(files["source/dependencies/input.bin"], content)
        self.assertEqual(config.read_bytes(), config_bytes)
        self.assertEqual(before, cp.files_under(caller / ".git"))

    def test_divergent_prompt_exports_and_unmanaged_internal_files_block_publish(self):
        (self.root / "prompts/M-1-follow-up.md").write_text("divergent", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "divergent"):
            self.snapshot()
        self.write_state()
        (self.root / "uncaptured.json").write_text("{}", encoding="utf-8")
        self.state["spec"] = str(self.root / "uncaptured.json")
        self.write_state()
        with self.assertRaisesRegex(ValueError, "not captured"):
            self.snapshot()

    def test_relative_spec_report_and_traversing_prompt_paths_fail_closed(self):
        for ref in ("../outside.md", "uncaptured.md"):
            self.state["spec"] = ref
            self.write_state()
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "absolute file path"):
                self.snapshot()
        self.state["spec"] = "https://example.test/issues/123"
        self.state["missions"][0]["prompt"]["file"] = "prompts/unused/../M-1-follow-up.md"
        self.write_state()
        with self.assertRaisesRegex(ValueError, "Unsafe relative path"):
            self.snapshot()

    def refs(self):
        return native_git("--git-dir", self.remote, "for-each-ref", "--format=%(refname) %(objectname)")

    def shared_publish(self, entry, expected="absent", root=None, adoption=None):
        return cp.publish(root or self.root, "tracker.html", [], str(self.remote), entry["branch"], expected,
                          entry["id"], adoption)

    def test_shared_first_publish_and_restored_republication_bind_identity(self):
        entry = self.catalog_entry()
        catalog_sha = self.put_catalog([entry])
        self.state["spec"] = entry["source"]
        self.write_state()
        before = self.refs()
        for selected, error in ((None, "requires --workstream-id"), ("missing/delivery", "Unknown workstream ID")):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, error):
                cp.publish(self.root, "tracker.html", [], str(self.remote), entry["branch"], "absent", selected)
            self.assertEqual(self.refs(), before)
        command = [sys.executable, "-B", str(SCRIPT), "publish", "--root", str(self.root),
                   "--remote", str(self.remote), "--branch", entry["branch"], "--expected", "absent",
                   "--workstream-id", entry["id"]]
        publication = json.loads(subprocess.check_output(command))
        _, files = cp.remote_snapshot(str(self.remote), entry["branch"], publication["commit"])
        manifest = cp.verify_files(files, entry["id"])
        self.assertEqual(manifest["workstream"], {"id": entry["id"], "source": entry["source"]})
        self.assertNotIn(str(self.remote), files[cp.MANIFEST].decode())
        destination = self.base / "shared restored"
        cp.restore(files, destination, entry["id"])
        shutil.rmtree(self.root)
        next_commit = self.shared_publish(entry, publication["commit"], destination)["commit"]
        _, fresh = cp.remote_snapshot(str(self.remote), entry["branch"], next_commit)
        self.assertEqual(cp.verify_files(fresh, entry["id"])["workstream"], manifest["workstream"])
        self.assertEqual(native_git("--git-dir", self.remote, "rev-parse", next_commit + "^"), publication["commit"])
        self.assertEqual(native_git("--git-dir", self.remote, "rev-parse", "main"), catalog_sha)

    def test_wrong_stream_branch_and_shared_omission_keep_refs_unchanged(self):
        alpha = self.catalog_entry("alpha", "recovery/alpha/delivery")
        beta = self.catalog_entry("beta", "recovery/beta/delivery")
        self.put_catalog([alpha, beta])
        commits = {}
        for entry in (alpha, beta):
            self.state["spec"] = entry["source"]
            self.write_state()
            commits[entry["id"]] = self.shared_publish(entry)["commit"]
        _, files = cp.remote_snapshot(str(self.remote), alpha["branch"])
        target = self.base / "restored alpha"
        cp.restore(files, target, alpha["id"])
        before = self.refs()
        cases = [(beta["id"], beta["branch"], "identity differs"),
                 (alpha["id"], beta["branch"], "registered branch"),
                 (None, beta["branch"], "requires --workstream-id")]
        for selected, branch, error in cases:
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, error):
                cp.publish(target, "tracker.html", [], str(self.remote), branch, commits[beta["id"]], selected)
            self.assertEqual(self.refs(), before)
        # A clean root cannot bypass the first binding's canonical source check.
        self.state["spec"] = alpha["source"]
        self.write_state()
        with self.assertRaisesRegex(ValueError, "selected spec source differs"):
            self.shared_publish(beta, commits[beta["id"]])
        self.assertEqual(self.refs(), before)
        # Matching current HTML is insufficient when the prior branch is another bound stream.
        native_git("--git-dir", self.remote, "update-ref", "refs/heads/" + alpha["branch"], commits[beta["id"]])
        before = self.refs()
        with self.assertRaisesRegex(ValueError, "prior branch checkpoint: workstream identity differs"):
            self.shared_publish(alpha, commits[beta["id"]])
        self.assertEqual(self.refs(), before)

    def test_prior_unbound_url_source_must_match_before_adoption(self):
        old = self.publish()["commit"]
        entry = self.catalog_entry()
        self.put_catalog([entry])
        self.state["spec"] = entry["source"]
        self.write_state()
        before = self.refs()
        with self.assertRaisesRegex(ValueError, "prior branch checkpoint: selected spec source differs"):
            self.shared_publish(entry, old)
        self.assertEqual(self.refs(), before)

    def test_matching_unbound_url_snapshot_can_acquire_shared_binding(self):
        entry = self.catalog_entry()
        self.state["spec"] = entry["source"]
        self.write_state()
        old = self.publish()["commit"]
        self.put_catalog([entry])
        new = self.shared_publish(entry, old)["commit"]
        _, files = cp.remote_snapshot(str(self.remote), entry["branch"], new)
        cp.verify_files(files, entry["id"])

    def test_local_spec_legacy_remains_readable_and_needs_explicit_binding_adoption(self):
        self.state["spec"] = str(self.root / "context.md")
        self.write_state()
        old = self.publish()["commit"]
        entry = dict(self.catalog_entry(), source="fixture://tickets/5", status="example")
        self.put_catalog([entry])
        _, files = cp.remote_snapshot(str(self.remote), entry["branch"], old)
        self.assertNotIn("workstream", cp.verify_files(files))
        target = self.base / "legacy local spec"
        receipt = cp.restore(files, target)
        self.assertTrue(any("identity is not established" in p for p in receipt["limitations"]))
        before = self.refs()
        for root in (self.root, target):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "local-spec.*explicit --adopt-workstream-source"):
                self.shared_publish(entry, old, root)
            self.assertEqual(self.refs(), before)
        with self.assertRaisesRegex(ValueError, "selected registered canonical source"):
            self.shared_publish(entry, old, target, "fixture://tickets/wrong")
        self.assertEqual(self.refs(), before)
        self.shared_publish(entry, old, target, entry["source"])

    def test_verified_bound_local_spec_restore_carries_identity_into_active_tracker(self):
        self.state["spec"] = str(self.root / "context.md")
        self.write_state()
        identity = {"id": "project/local", "source": "fixture://spec/verified-local-mapping"}
        files = cp.make_snapshot(self.root, "tracker.html", [], identity, identity["source"])
        exact_source = files["source/tracker.html"]
        self.assertNotIn("workstream", cp.state_from(exact_source, self.tracker)[1])
        destination = self.base / "bound restored"
        cp.restore(files, destination, identity["id"])
        active = cp.state_from((destination / "tracker.html").read_bytes(), self.tracker)[1]
        self.assertEqual(active["workstream"], identity)
        self.assertEqual((destination / ".checkpoint/source/tracker.html").read_bytes(), exact_source)
        helper = destination / ".checkpoint/tools/bootstrap.py"
        args = [sys.executable, "-B", str(helper), "bind", str(destination),
                "--tracker", str(destination / "tracker.html"), "--id"]
        wrong = subprocess.run(args + ["other/project"], capture_output=True, text=True)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertFalse((destination / "AGENTS.md").exists())
        result = subprocess.run(args + [identity["id"]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["selected"]["source"], identity["source"])
        self.assertEqual(files["source/tracker.html"], exact_source)

    def test_conflicting_active_identity_is_rejected_before_restore(self):
        self.state["workstream"] = {"id": "other/project", "source": "fixture://spec/other"}
        self.write_state()
        identity = {"id": "project/current", "source": self.state["spec"]}
        with self.assertRaisesRegex(ValueError, "Active tracker workstream differs"):
            cp.make_snapshot(self.root, "tracker.html", [], identity)

    def test_expected_workstream_cli_checks_before_destination_creation(self):
        entry = self.catalog_entry()
        self.state["spec"] = entry["source"]
        self.write_state()
        self.put_catalog([entry])
        self.shared_publish(entry)
        clone = self.base / "bound clone"
        native_git("clone", "--quiet", "--branch", entry["branch"], self.remote, clone)
        for command in ("verify", "restore"):
            destination = self.base / "wrong identity must not appear"
            args = [sys.executable, "-B", str(clone / "tools/checkpoint.py"), command,
                    "--snapshot", str(clone), "--expected-workstream-id", "beta/delivery"]
            if command == "restore":
                args += ["--destination", str(destination)]
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Expected workstream ID differs", result.stderr)
            self.assertFalse(destination.exists())
        result = subprocess.run([sys.executable, "-B", str(clone / "tools/checkpoint.py"), "verify",
                                 "--snapshot", str(clone), "--expected-workstream-id", entry["id"]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_unbound_v1_metadata_is_unknown_and_missing_legacy_proof_still_fails(self):
        files = self.snapshot()
        manifest = json.loads(files[cp.MANIFEST])
        for key in ("workstream", "executable", "dependency_scan", "evidence_origins"):
            manifest.pop(key, None)
        self.resign(files, manifest)
        manifest = cp.verify_files(files)
        target = self.base / "old v1"
        receipt = cp.restore(files, target)
        self.assertGreaterEqual(len(receipt["limitations"]), 3)
        self.assertTrue(any("historical attachment inventory is unknown" in p for p in receipt["limitations"]))
        self.assertTrue(any("executable flags are unknown" in p for p in receipt["limitations"]))
        with self.assertRaisesRegex(ValueError, "unbound"):
            cp.verify_files(files, "alpha/delivery")
        missing_target = self.base / "no legacy success"
        files["source/evidence/M-1/report.md"] = b"See [proof](missing-proof.log).\n"
        self.resign(files)
        for action in (lambda: cp.verify_files(files), lambda: cp.restore(files, missing_target)):
            with self.assertRaisesRegex(ValueError, "evidence/M-1/report.md.*missing-proof.log"):
                action()
        self.assertFalse(missing_target.exists())

    def test_actual_previous_writer_versions_verify_restore_and_republish(self):
        fixtures = Path(__file__).with_name("fixtures")
        versions = {
            "original": ("tracker-v1-original.py.txt", "1629bbf4472608e024ec1a80c894146bc03b5c89a34602a2bcb1e2d1c096a2a5"),
            "r1": ("tracker-v1-before-atomic-export.py.txt", "2bfac353909a2565ccf3eb77c99c964fe7e23b238373ae3a3e34ef472f0e8c4e"),
            "before-iterative": ("tracker-v1-before-iterative-validation.py.txt", "a2a219b4b57749b45b43ef3ce6807593859ef2e7208f4fbad5233c058590ab66"),
        }
        for version, (filename, sha) in versions.items():
            with self.subTest(version=version):
                # Actual earlier writer bytes, read as data and never imported.
                old_writer = (fixtures / filename).read_bytes()
                self.assertEqual(cp.digest(old_writer), sha)
                files = self.snapshot()
                files["tools/tracker.py"] = old_writer
                manifest = json.loads(files[cp.MANIFEST])
                for key in ("workstream", "executable", "dependency_scan", "evidence_origins"):
                    manifest.pop(key, None)
                self.resign(files, manifest)
                cp.verify_files(files)
                restored = self.base / ("real-writer-" + version)
                cp.restore(files, restored)
                self.assertEqual((restored / ".checkpoint/tools/tracker.py").read_bytes(), old_writer)
                receipt = cp.publish(restored, "tracker.html", [], str(self.remote),
                                     "recovery/legacy-" + version, "absent")
                _, uploaded = cp.remote_snapshot(str(self.remote), receipt["branch"], receipt["commit"])
                cp.verify_files(uploaded)
                active = cp.state_from(uploaded["source/tracker.html"], self.tracker)[1]
                self.assertEqual(active["missions"][0]["dispatch"], self.state["missions"][0]["dispatch"])
                self.assertEqual(active["missions"][0]["prompt"]["delivery"], "unknown")
                # Compatibility remains exact; changed archived code stays rejected.
                files["tools/tracker.py"] += b"\n# Unrecognized writer variation.\n"
                self.resign(files)
                with self.assertRaisesRegex(ValueError, "Tracker helper differs"):
                    cp.verify_files(files)

    def test_binding_selector_and_identity_metadata_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "requires registry.json"):
            cp.publish(self.root, "tracker.html", [], str(self.remote), "recovery/main", "absent", "alpha/delivery")
        for source in ("https://secret@example.test/spec", "https://example.test/spec?token=secret",
                       "file:///local/spec", str(self.root / "context.md")):
            files = self.snapshot()
            manifest = json.loads(files[cp.MANIFEST])
            manifest["workstream"] = {"id": "alpha/delivery", "source": source}
            self.resign(files, manifest)
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Canonical workstream source"):
                cp.restore(files, self.base / "invalid identity")
        self.assertEqual(self.refs(), "")

    def test_report_attachment_omission_rejected_and_same_location_include_restores_links(self):
        artifact = self.root / "artifacts/command.log"
        artifact.parent.mkdir()
        artifact.write_bytes(b"exact command evidence\r\n")
        report = self.root / "evidence/M-1/report.md"
        content = b"See [command output](../../artifacts/command.log).\r\n"
        report.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "evidence/M-1/report.md.*artifacts/command.log"):
            self.publish()
        self.assertEqual(self.refs(), "")
        result = self.publish(includes=[f"{artifact.parent}=artifacts"])
        _, files = cp.remote_snapshot(str(self.remote), "recovery/main", result["commit"])
        shutil.rmtree(self.root)
        destination = self.base / "linked restore"
        cp.restore(files, destination)
        self.assertEqual((destination / "evidence/M-1/report.md").read_bytes(), content)
        self.assertEqual((destination / "evidence/M-1/../../artifacts/command.log").read_bytes(), b"exact command evidence\r\n")
        cp.verify_files(cp.local_snapshot(destination / ".checkpoint"))

    def test_transitive_report_links_require_each_attachment_and_external_locations_are_restricted(self):
        report = self.root / "evidence/M-1/report.md"
        report.write_text("[details](details.md)\n", encoding="utf-8")
        (report.parent / "details.md").write_text("[missing](proof.log)\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence/M-1/details.md.*proof.log"):
            self.snapshot()
        (report.parent / "proof.log").write_text("proof\n", encoding="utf-8")
        cp.verify_files(self.snapshot())
        external = self.base / "external.txt"
        external.write_text("external\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SAME source-relative location"):
            self.snapshot([f"{external}=artifacts/external.txt"])

    def test_json_receipt_host_provenance_is_opaque_but_its_markdown_link_is_checked(self):
        report = self.root / "evidence/M-1/report.md"
        report.write_text("See [receipt](independent-verification.json).\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "report.md.*independent-verification.json"):
            self.snapshot()
        receipt_path = report.parent / "independent-verification.json"
        receipt_bytes = cp.json_bytes({"source_host_root": "/lost/host/scratch", "old_command": "read /lost/host/tool"})
        receipt_path.write_bytes(receipt_bytes)
        files = self.snapshot()
        manifest = cp.verify_files(files)
        self.assertIn("evidence/M-1/independent-verification.json", manifest["dependency_scan"]["opaque"])
        target = self.base / "opaque receipt restore"
        receipt = cp.restore(files, target)
        self.assertEqual((target / "evidence/M-1/independent-verification.json").read_bytes(), receipt_bytes)
        self.assertTrue(any("Opaque files" in p and "independent-verification.json" in p for p in receipt["limitations"]))
        # Absence of the new scan marker does not hide the current inspection limit.
        manifest.pop("dependency_scan")
        self.resign(files, manifest)
        self.assertTrue(any("Opaque files" in p for p in cp.limitations(cp.verify_files(files), files)))
        report.write_text("[missing](missing.log)\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "report.md.*missing.log"):
            self.snapshot()

    def test_absolute_evidence_bytes_and_original_mappings_survive_republication(self):
        report = self.root / "evidence/M-1/report.md"
        attachment = self.root / "evidence/M-1/command.log"
        attachment.write_bytes(b"proof\r\n")
        external = self.base / "external evidence"
        external.mkdir()
        (external / "extra.log").write_bytes(b"external proof\n")
        content = (f"[command]({attachment})\r\n[extra]({external}/extra.log)\r\n").encode()
        report.write_bytes(content)
        files = self.snapshot([f"{external}=dependencies/external"])
        shutil.rmtree(self.root)
        shutil.rmtree(external)
        for index in range(3):
            target = self.base / f"immutable restore {index}"
            cp.verify_files(files)
            cp.restore(files, target)
            self.assertEqual((target / "evidence/M-1/report.md").read_bytes(), content)
            self.assertEqual((target / ".checkpoint/source/evidence/M-1/report.md").read_bytes(), content)
            files = cp.make_snapshot(target, "tracker.html", [])
            shutil.rmtree(target)
        cp.verify_files(files)

    def test_included_report_is_immutable_even_outside_evidence_directory(self):
        report = self.base / "report.md"
        content = f"[tracker]({self.root}/tracker.html)\r\n".encode()
        report.write_bytes(content)
        self.state["missions"][0]["report"] = str(report)
        self.state["missions"][0]["evidence"] = [str(report)]
        self.write_state()
        files = self.snapshot([f"{report}=dependencies/report.md"])
        target = self.base / "included report restore"
        cp.restore(files, target)
        self.assertEqual((target / "dependencies/report.md").read_bytes(), content)

    def test_factual_included_markdown_evidence_is_preserved_as_bytes(self):
        proof = self.base / "proof.md"
        content = f"[original tracker]({self.root}/tracker.html)\r\n".encode()
        proof.write_bytes(content)
        self.state["missions"][0]["evidence"].append(f"[proof]({proof})")
        self.write_state()
        files = self.snapshot([f"{proof}=dependencies/proof.md"])
        target = self.base / "factual included evidence"
        cp.restore(files, target)
        self.assertEqual((target / "dependencies/proof.md").read_bytes(), content)

    def test_executable_git_active_and_archive_modes_survive_source_removal_and_republication(self):
        tool = self.base / "tool"
        tool.mkdir()
        script = tool / "run.sh"
        script.write_bytes(b"#!/bin/sh\nprintf 'checkpoint executable works\\n'\n")
        script.chmod(0o4751)  # Normalize setuid and unusual permissions away.
        data = tool / "data.txt"
        data.write_bytes(b"plain input\n")
        data.chmod(0o666)
        (self.root / "context.md").write_text(f"Run `{script}`.\n", encoding="utf-8")
        publication = self.publish(includes=[f"{tool}=dependencies/tool"])
        listing = native_git("--git-dir", self.remote, "ls-tree", "-r", publication["commit"])
        self.assertRegex(listing, r"100755 blob [a-f0-9]+\tsource/dependencies/tool/run.sh")
        self.assertRegex(listing, r"100644 blob [a-f0-9]+\tsource/dependencies/tool/data.txt")
        clone = self.base / "executable clone"
        native_git("clone", "--quiet", "--branch", "recovery/main", self.remote, clone)
        shutil.rmtree(self.root)
        shutil.rmtree(tool)
        target = self.base / "executable restored"
        result = subprocess.run([sys.executable, "-B", str(clone / "tools/checkpoint.py"), "restore",
                                 "--snapshot", str(clone), "--destination", str(target)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for folder in (target, target / ".checkpoint/source"):
            executable = folder / "dependencies/tool/run.sh"
            self.assertEqual(executable.stat().st_mode & 0o7777, 0o755)
            self.assertEqual((folder / "dependencies/tool/data.txt").stat().st_mode & 0o7777, 0o644)
            self.assertEqual(subprocess.check_output([str(executable)]), b"checkpoint executable works\n")
        self.assertIn(str(target / "dependencies/tool/run.sh"), (target / "context.md").read_text())
        cp.publish(target, "tracker.html", [], str(self.remote), "recovery/main", publication["commit"])
        (clone / "source/dependencies/tool/run.sh").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "Changed checkpoint executable flag"):
            cp.local_snapshot(clone)

    def test_mode_metadata_is_regular_boolean_inventory_and_git_mode_mismatch_is_rejected(self):
        for metadata in ({"tracker.html": True}, {"tracker.html": "4755"}, [], None):
            files = self.snapshot()
            manifest = json.loads(files[cp.MANIFEST])
            manifest["executable"] = metadata
            self.resign(files, manifest)
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, "executable metadata"):
                cp.verify_files(files)
        files = self.snapshot()
        manifest = json.loads(files[cp.MANIFEST])
        manifest["executable"]["context.md"] = True
        self.resign(files, manifest)
        repo = self.base / "incorrect git modes"
        cp.init_repo(repo)
        cp.write_files(repo, files)  # All files are 644, contrary to the declared flag.
        cp.git(repo, "add", "--all")
        cp.git(repo, "commit", "--quiet", "-m", "Invalid mode fixture")
        cp.git(repo, "push", "--quiet", str(self.remote), "HEAD:refs/heads/wrong-modes")
        with self.assertRaisesRegex(ValueError, "Changed checkpoint executable flag: source/context.md"):
            cp.remote_snapshot(str(self.remote), "wrong-modes")


if __name__ == "__main__":
    unittest.main()
