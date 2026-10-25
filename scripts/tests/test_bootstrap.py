"""Local bootstrap discovery, identity, byte preservation and writer safety."""

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap.py"
spec = importlib.util.spec_from_file_location("bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bootstrap-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace with spaces"
        self.root.mkdir()
        self.agents = self.root / "AGENTS.md"
        self.source = "fixture://spec/current"
        self.html = self.make_tracker("active/tracker.html")

    def make_tracker(self, relative, source=None, workstream=None):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"title": "Local only", "spec": source or self.source, "scope": "One workstream",
                "updated_at": "2026-10-23T00:00:00Z", "next_action": "Reconcile pending sends",
                "journal": [], "decisions": [], "missions": []}
        if workstream is not None:
            data["workstream"] = workstream
        self.write_tracker(path, data)
        return path

    def write_tracker(self, path, data):
        path.write_text('<script type="application/json" id="tracker-data">'
                        + json.dumps(data) + "</script>", encoding="utf-8")

    def data(self, path=None):
        text = (path or self.html).read_text()
        return json.loads(text.split(">", 1)[1].rsplit("<", 1)[0])

    def sha(self):
        return hashlib.sha256(self.agents.read_bytes()).hexdigest()

    def bind(self, path=None, workstream_id="project/current", expected=None):
        return bootstrap.bind(self.root, str(path or self.html), workstream_id, expected)

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True, timeout=10)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        json.loads(result.stderr)
        return result

    def test_lost_chat_uses_only_registered_tracker_not_newest_physical_file(self):
        result = self.cli("bind", self.root, "--tracker", "active/tracker.html", "--id", "project/current")
        decoy = self.make_tracker("newer/tracker.html", "fixture://spec/other")
        os.utime(decoy, (2_000_000_000, 2_000_000_000))
        before = {p: p.read_bytes() for p in (self.html, decoy, self.agents)}
        located = self.cli("locate", self.root)
        self.assertEqual(located, result)
        self.assertEqual(located["selected"], {
            "id": "project/current", "tracker": "active/tracker.html", "source": self.source,
            "revision_sha256": hashlib.sha256(before[self.html]).hexdigest()})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_two_registrations_are_ambiguous_until_exact_id_is_given(self):
        first = self.bind()
        second_path = self.make_tracker("other.html", "fixture://spec/second")
        self.bind(second_path, "project/second", first["agents_sha256"])
        result = self.cli("locate", self.root)
        self.assertIsNone(result["selected"])
        self.assertEqual([v["id"] for v in result["choices"]], ["project/current", "project/second"])
        self.assertEqual(self.cli("locate", self.root, "--id", "project/second")["selected"]["tracker"], "other.html")
        self.cli("locate", self.root, "--id", "project/absent", success=False)

    def test_existing_agents_requires_sha_and_stale_sha_leaves_bytes_untouched(self):
        self.agents.write_bytes(b"Original instruction without newline")
        old = self.sha()
        self.agents.write_bytes(self.agents.read_bytes() + b"; concurrent human instruction")
        before = self.agents.read_bytes()
        for expected in (None, old, "bad-sha"):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                self.bind(expected=expected)
            self.assertEqual(self.agents.read_bytes(), before)
        self.bind(expected=self.sha())
        self.assertTrue(self.agents.read_bytes().startswith(before))

    def test_unregistered_agents_returns_sha_for_first_binding(self):
        original = b"\xffExisting workspace instructions without final newline"
        self.agents.write_bytes(original)
        result = self.cli("locate", self.root)
        self.assertIsNone(result["selected"])
        self.assertEqual(result["choices"], [])
        self.assertEqual(result["agents_sha256"], self.sha())
        self.assertEqual(self.agents.read_bytes(), original)
        self.bind(expected=result["agents_sha256"])
        self.assertEqual(bootstrap.locate(self.root)["selected"]["id"], "project/current")

    def test_relocated_bundle_uses_sibling_helpers_without_scripts_directory(self):
        for layout in ("shared-repo/tools", "restored/.checkpoint/tools"):
            with self.subTest(layout=layout):
                tools = Path(self.temp.name) / layout
                tools.mkdir(parents=True)
                for name in ("bootstrap.py", "resume.py", "tracker.py"):
                    shutil.copyfile(SCRIPT.with_name(name), tools / name)
                relocated = tools / "bootstrap.py"
                self.assertFalse((tools.parent / "scripts").exists())
                self.assertFalse((tools.parent / "SKILL.md").exists())
                result = subprocess.run([sys.executable, "-B", str(relocated), "bind", str(self.root),
                                         "--tracker", str(self.html), "--id", "project/current"],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                raw = self.agents.read_bytes()
                self.assertIn(str(relocated).encode(), raw)
                self.assertIn(str(tools / "resume.py").encode(), raw)
                result = subprocess.run([sys.executable, "-B", str(relocated), "locate", str(self.root)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["selected"]["source"], self.source)
                self.agents.unlink()

    def test_instruction_falls_back_to_shared_docs_when_installed_skill_is_unavailable(self):
        tools = Path(self.temp.name) / "bundle/tools"
        tools.mkdir(parents=True)
        docs = tools.parent / "docs/long-context.md"
        docs.parent.mkdir()
        docs.write_text("Recovery documentation")
        with mock.patch.object(bootstrap, "SCRIPT", tools / "bootstrap.py"), \
                mock.patch.object(bootstrap.Path, "home", return_value=Path(self.temp.name) / "empty-home"), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(self.temp.name) / "empty-codex")}):
            self.assertIn(str(docs).encode(), bootstrap.instruction())
            docs.unlink()
            self.assertIn(b"available installed", bootstrap.instruction())

    def test_arbitrary_bytes_no_final_newline_prefix_suffix_and_mode_survive(self):
        prefix = b"\xff\x00original\r\nbytes without final newline"
        self.agents.write_bytes(prefix)
        self.agents.chmod(0o640)
        self.bind(expected=self.sha())
        self.assertTrue(self.agents.read_bytes().startswith(prefix))
        suffix = b"\n\xfehuman suffix without final newline"
        self.agents.write_bytes(self.agents.read_bytes() + suffix)
        second = self.make_tracker("second.html", "fixture://spec/second")
        self.bind(second, "project/second", self.sha())
        raw = self.agents.read_bytes()
        self.assertTrue(raw.startswith(prefix))
        self.assertTrue(raw.endswith(suffix))
        self.assertEqual(stat.S_IMODE(self.agents.stat().st_mode), 0o640)
        self.assertEqual(raw.count(bootstrap.START), 1)

    def test_identical_binding_is_idempotent_but_still_requires_current_sha(self):
        result = self.bind()
        before, inode = self.agents.read_bytes(), self.agents.stat().st_ino
        again = self.bind(expected=result["agents_sha256"])
        self.assertEqual(result, again)
        self.assertEqual(self.agents.read_bytes(), before)
        self.assertEqual(self.agents.stat().st_ino, inode)
        with self.assertRaises(ValueError):
            self.bind(expected="0" * 64)

    def test_id_and_tracker_path_cannot_be_silently_rebound(self):
        self.bind()
        other = self.make_tracker("other.html")
        before = self.agents.read_bytes()
        for path, wid in ((other, "project/current"), (self.html, "project/different")):
            with self.subTest(path=path, wid=wid), self.assertRaisesRegex(ValueError, "differs"):
                self.bind(path, wid, self.sha())
            self.assertEqual(self.agents.read_bytes(), before)

    def test_changed_source_fails_and_never_falls_back_to_another_tracker(self):
        self.bind()
        self.make_tracker("decoy.html")
        data = self.data()
        data["spec"] = "fixture://spec/changed"
        self.write_tracker(self.html, data)
        before = self.agents.read_bytes()
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            bootstrap.locate(self.root)
        with self.assertRaises(ValueError):
            self.bind(expected=self.sha())
        self.assertEqual(self.agents.read_bytes(), before)

    def test_explicit_workstream_id_source_and_spec_must_agree(self):
        data = self.data()
        data["workstream"] = {"id": "project/current", "source": self.source}
        self.write_tracker(self.html, data)
        self.bind()
        for replacement, spec_source in (({"id": "project/changed", "source": self.source}, self.source),
                                         ({"id": "project/current", "source": "fixture://spec/changed"}, self.source),
                                         (data["workstream"], "fixture://spec/changed"),
                                         (None, self.source)):
            changed = dict(data, workstream=replacement, spec=spec_source)
            self.write_tracker(self.html, changed)
            with self.subTest(replacement=replacement, spec=spec_source), self.assertRaises(ValueError):
                bootstrap.locate(self.root)

    def test_legacy_url_and_local_specs_keep_verifiable_source_locations(self):
        self.bind()
        self.assertEqual(bootstrap.locate(self.root)["selected"]["source"], self.source)
        self.agents.unlink()
        local = self.root / "spec.md"
        local.write_text("Local specification")
        self.write_tracker(self.html, dict(self.data(), spec=str(local)))
        registered = self.bind()
        self.assertEqual(registered["selected"]["source"], "workspace:spec.md")
        self.assertEqual(bootstrap.locate(self.root)["selected"]["source"], "workspace:spec.md")
        self.agents.unlink()
        self.assertFalse(self.agents.exists())
        data = self.data()
        data["workstream"] = {"id": "project/current", "source": self.source}
        self.write_tracker(self.html, data)
        self.bind()
        local.unlink()
        with self.assertRaisesRegex(ValueError, "Missing file"):
            bootstrap.locate(self.root)

    def test_relative_local_spec_survives_workspace_move_and_detects_source_change(self):
        (self.root / "spec.md").write_text("Local specification")
        self.write_tracker(self.html, dict(self.data(), spec="../spec.md"))
        self.bind()
        moved = self.root.parent / "moved workspace"
        shutil.copytree(self.root, moved)
        self.assertEqual(bootstrap.locate(moved)["selected"]["source"], "workspace:spec.md")
        (moved / "other.md").write_text("Other specification")
        self.write_tracker(moved / "active/tracker.html", dict(self.data(), spec="../other.md"))
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            bootstrap.locate(moved)

    def test_unbound_local_spec_outside_workspace_requires_explicit_identity(self):
        outside = self.root.parent / "spec.md"
        outside.write_text("External local spec")
        self.write_tracker(self.html, dict(self.data(), spec=str(outside)))
        with self.assertRaisesRegex(ValueError, "inside the workspace"):
            self.bind()
        self.assertFalse(self.agents.exists())

    def test_unsafe_or_noncanonical_paths_refused(self):
        outside = self.root.parent / "outside.html"
        outside.write_bytes(self.html.read_bytes())
        for path in (outside, "../outside.html", "active/../active/tracker.html", "active//tracker.html", "."):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.bind(path)
        self.assertFalse(self.agents.exists())

    def test_missing_tracker_or_agents_never_falls_back(self):
        with self.assertRaisesRegex(ValueError, "Missing file"):
            bootstrap.locate(self.root)
        self.bind()
        self.html.unlink()
        with self.assertRaisesRegex(ValueError, "Missing file"):
            bootstrap.locate(self.root)

    def test_symlink_file_directory_workspace_and_agents_refused(self):
        link = self.root / "link.html"
        link.symlink_to(self.html)
        directory = self.root / "linked"
        directory.symlink_to(self.html.parent, target_is_directory=True)
        workspace_link = self.root.parent / "linked-workspace"
        workspace_link.symlink_to(self.root, target_is_directory=True)
        for path in (link, directory / "tracker.html"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Symlink"):
                self.bind(path)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            bootstrap.bind(workspace_link, "active/tracker.html", "project/current")
        external = self.root.parent / "external-agents"
        external.write_bytes(b"Keep me")
        self.agents.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.bind()
        with self.assertRaisesRegex(ValueError, "Symlink"):
            bootstrap.locate(self.root)
        self.assertEqual(external.read_bytes(), b"Keep me")

    def test_selected_tracker_symlink_hardlink_and_fifo_refused(self):
        self.bind()
        original = self.html.read_bytes()
        other = self.root / "other.html"
        other.write_bytes(original)
        self.html.unlink()
        self.html.symlink_to(other)
        with self.assertRaises(ValueError):
            bootstrap.locate(self.root)
        self.html.unlink()
        os.link(other, self.html)
        with self.assertRaises(ValueError):
            bootstrap.locate(self.root)
        self.html.unlink()
        os.mkfifo(self.html)
        self.cli("locate", self.root, success=False)

    def test_malformed_duplicate_blocks_and_json_refused_without_changes(self):
        self.bind()
        good = self.agents.read_bytes()
        entries, _ = bootstrap.parse_block(good)
        duplicate_id = entries + [dict(entries[0], tracker="other.html")]
        duplicate_path = entries + [dict(entries[0], id="project/other")]
        with_extra_state = [dict(entries[0], status="running")]
        cases = [good + good, good.replace(bootstrap.END, b""),
                 good.replace(bootstrap.START, b"<!-- orchestrate-tickets:bootstrap:broken -->"),
                 good.replace(b'"schema":', b'"schema":NaN,"schema":'),
                 good.replace(b'"locators":', b'"locators":[],"locators":'),
                 good.replace(b'"schema":', b'"schema"!'),
                 bootstrap.block_bytes(duplicate_id), bootstrap.block_bytes(duplicate_path),
                 bootstrap.block_bytes(with_extra_state)]
        for raw in cases:
            with self.subTest(raw=raw):
                self.agents.write_bytes(raw)
                with self.assertRaises(ValueError):
                    bootstrap.locate(self.root)
                with self.assertRaises(ValueError):
                    self.bind(expected=self.sha())
                self.assertEqual(self.agents.read_bytes(), raw)

    def test_invalid_tracker_json_and_validation_are_not_bypassed(self):
        original = self.html.read_bytes()
        for raw in (original.replace(b'"title":', b'"title":"duplicate","title":'),
                    original.replace(b'"missions": []', b'"missions": {}'),
                    original.replace(b'"title":', b'"title"!'), original + original):
            self.html.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.bind()
            self.assertFalse(self.agents.exists())

    def test_workspace_tracker_cannot_supply_executable_helper(self):
        (self.html.parent / "tracker.py").write_text("raise RuntimeError('untrusted helper')")
        self.bind()
        self.assertEqual(bootstrap.locate(self.root)["selected"]["source"], self.source)

    def test_block_has_only_stable_locators_and_no_sidecar_is_created(self):
        before = set(self.root.rglob("*"))
        self.bind()
        added = set(self.root.rglob("*")) - before
        self.assertEqual(added, {self.agents})
        entries, _ = bootstrap.parse_block(self.agents.read_bytes())
        self.assertEqual(set(entries[0]), {"id", "tracker", "source"})
        self.assertIn(b"reconcile recorded pending sends before action", self.agents.read_bytes())
        self.assertIn(str(SCRIPT.with_name("resume.py")).encode(), self.agents.read_bytes())

    def test_directory_lock_rejects_another_process_without_writing(self):
        self.bind()
        before = self.agents.read_bytes()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli("bind", self.root, "--tracker", self.html, "--id", "project/current",
                              "--expected", self.sha(), success=False)
            self.assertIn("workspace lock", result.stderr)
        finally:
            os.close(fd)
        self.assertEqual(self.agents.read_bytes(), before)

    def test_concurrent_change_during_validation_survives_and_temp_is_cleaned(self):
        self.agents.write_bytes(b"Before without newline")
        expected = self.sha()
        fsync = os.fsync
        changed = b"Concurrent human instruction without newline"
        def change_during_write(fd):
            self.agents.write_bytes(changed)
            fsync(fd)
        with mock.patch.object(bootstrap.os, "fsync", side_effect=change_during_write):
            with self.assertRaisesRegex(ValueError, "changed during bind"):
                self.bind(expected=expected)
        self.assertEqual(self.agents.read_bytes(), changed)
        self.assertEqual(list(self.root.glob(".bootstrap-*")), [])

    def test_two_processes_cannot_lose_each_others_binding(self):
        prefix = b"Unrelated instructions without newline"
        self.agents.write_bytes(prefix)
        expected = self.sha()
        second = self.make_tracker("second.html", "fixture://spec/second")
        jobs = [(self.html, "project/current"), (second, "project/second")]
        processes = [subprocess.Popen(
            [sys.executable, "-B", str(SCRIPT), "bind", str(self.root), "--tracker", str(path),
             "--id", wid, "--expected", expected], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True) for path, wid in jobs]
        results = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], results)
        loser = next(i for i, process in enumerate(processes) if process.returncode)
        self.bind(*jobs[loser], expected=self.sha())
        located = bootstrap.locate(self.root)
        self.assertEqual(len(located["choices"]), 2)
        self.assertIsNone(located["selected"])
        self.assertTrue(self.agents.read_bytes().startswith(prefix))

    def test_success_syncs_file_before_replace_then_directory(self):
        events = []
        fsync, replace = os.fsync, os.replace
        def record_sync(fd):
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            fsync(fd)
        def record_replace(source, target):
            events.append("replace")
            replace(source, target)
        with mock.patch.object(bootstrap.os, "fsync", side_effect=record_sync), \
                mock.patch.object(bootstrap.os, "replace", side_effect=record_replace):
            self.bind()
        self.assertEqual(events, ["file", "replace", "directory"])

    def test_replace_failure_keeps_original_and_cleans_temporary(self):
        self.agents.write_bytes(b"Original bytes")
        with mock.patch.object(bootstrap.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.bind(expected=self.sha())
        self.assertEqual(self.agents.read_bytes(), b"Original bytes")
        self.assertEqual(list(self.root.glob(".bootstrap-*")), [])


if __name__ == "__main__":
    unittest.main()
