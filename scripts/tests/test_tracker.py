"""Writer isolation and optimistic concurrency at the public tracker interface."""

import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "tracker.py"
spec = importlib.util.spec_from_file_location("tracker_writer", SCRIPT)
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tracker-tests-")
        self.root = Path(self.temporary.name)
        self.html = self.root / "tracker.html"
        self.run_cli("init")

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, command, *args, success=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), command, str(self.html), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def read(self):
        return json.loads(self.run_cli("read").stdout)

    def save(self, data, success=True, *args):
        source = self.root / "editing.json"
        source.write_text(json.dumps(data), encoding="utf-8")
        return self.run_cli("save", "--data", str(source), *args, success=success)

    def prompt(self, data, text="Implement and report."):
        data["missions"] = [{"id": "M1", "title": "M1", "ticket": "fixture://M1", "status": "ready",
                             "criteria": [], "depends_on": [],
                             "prompt": {"kind": "implementation", "file": "prompts/M1.md",
                                        "delivery": "prepared", "text": text}}]

    def test_round_trip_exports_prompt_without_persisting_edit_metadata(self):
        data = self.read()
        self.prompt(data)
        self.save(data)
        self.assertNotIn(tracker.BASE_REVISION, tracker.read_state(self.html)[1])
        self.assertEqual((self.root / "prompts/M1.md").read_text(), "Implement and report.\n")
        self.run_cli("check")

    def test_stale_editor_cannot_erase_a_recorded_dispatch(self):
        first, second = self.read(), self.read()
        first["journal"].append("request-7 accepted; keep its owner.")
        self.save(first)
        accepted = self.html.read_bytes()
        self.prompt(second)
        result = self.save(second, success=False)
        self.assertIn("Stale tracker revision", result.stderr)
        self.assertEqual(self.html.read_bytes(), accepted)
        self.assertFalse((self.root / "prompts/M1.md").exists())
        # Reconciliation is possible using a fresh read and retaining its facts.
        reconciled = self.read()
        self.prompt(reconciled)
        self.save(reconciled)
        self.assertEqual(self.read()["journal"], first["journal"])

    def test_in_flight_writer_excludes_another_process(self):
        data = self.read()
        with tracker.writer_lock(self.html):
            result = self.save(data, success=False)
            self.assertIn("Another writer", result.stderr)
        self.save(data)

    def test_missing_or_conflicting_base_is_rejected(self):
        data = self.read()
        base = data.pop(tracker.BASE_REVISION)
        self.assertIn("base revision", self.save(data, success=False).stderr)
        self.save(data, True, "--expected", base)
        current = self.read()
        self.assertIn("differs", self.save(current, False, "--expected", "0" * 64).stderr)

    def test_layout_edit_and_not_just_json_invalidates_old_base(self):
        data = self.read()
        self.html.write_bytes(self.html.read_bytes() + b"\n<!-- concurrent layout edit -->\n")
        self.assertIn("Stale tracker revision", self.save(data, success=False).stderr)

    def test_existing_prompt_remains_immutable_and_failed_save_preserves_html(self):
        data = self.read()
        self.prompt(data)
        self.save(data)
        data = self.read()
        self.prompt(data, "Different payload.")
        before = self.html.read_bytes()
        self.assertIn("Preserve existing prompt", self.save(data, success=False).stderr)
        self.assertEqual(self.html.read_bytes(), before)
        self.assertEqual((self.root / "prompts/M1.md").read_text(), "Implement and report.\n")

    def test_interrupted_export_never_exposes_partial_prompt_and_can_resume(self):
        data = self.read()
        self.prompt(data, "Report exact evidence.\n" * 1000)
        editing = self.root / "pending.json"
        editing.write_text(json.dumps(data))
        before = self.html.read_bytes()
        injector = ("import resource, runpy, signal, sys\n"
                    "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
                    "resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))\n"
                    "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
                    "sys.argv = [sys.argv[1], 'save', sys.argv[2], '--data', sys.argv[3]]\n"
                    "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        child = subprocess.run([sys.executable, "-B", "-c", injector, str(SCRIPT),
                                str(self.html), str(editing)], capture_output=True)
        self.assertEqual(child.returncode, -signal.SIGXFSZ)
        self.assertEqual(self.html.read_bytes(), before)
        self.assertFalse((self.root / "prompts/M1.md").exists())
        self.save(data)
        self.run_cli("check")
        self.assertEqual((self.root / "prompts/M1.md").read_text(), data["missions"][0]["prompt"]["text"])

    def test_iterative_dependency_validation_retains_cycle_unknown_and_ready_checks(self):
        data = self.read()
        def mission(mid, dependencies, status="to-prepare"):
            return {"id":mid,"title":mid,"ticket":"fixture://"+mid,"status":status,
                    "depends_on":dependencies,"criteria":[]}
        for missions, message in (([mission("M1", ["M2"]), mission("M2", ["M1"])], "Dependency cycle"),
                                   ([mission("M1", ["missing"])], "Unknown dependency"),
                                   ([mission("M1", ["M2"], "ready"), mission("M2", [])], "Unmet prerequisite")):
            data["missions"] = missions
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                tracker.validate(data, self.root)


if __name__ == "__main__":
    unittest.main()
