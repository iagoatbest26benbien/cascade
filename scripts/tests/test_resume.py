"""Bounded model-visible reads over large canonical state; no external services."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "resume.py"
spec = importlib.util.spec_from_file_location("resume_view", SCRIPT)
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)
spec = importlib.util.spec_from_file_location("resume_checkpoint", SCRIPT.with_name("checkpoint.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-tests-")
        self.root = Path(self.temp.name)
        self.html = self.root / "tracker.html"
        self.tracker, _ = cp.tracker_module()
        self.template = (SCRIPT.parent.parent / "assets/tracker.html").read_text()
        self.data = {"title": "Long delivery", "spec": "fixture://spec/current", "scope": "Current scope",
                     "updated_at": "2026-10-23T20:00:00+00:00", "next_action": "Reconcile before sending.",
                     "decisions": [], "journal": [], "missions": [self.mission("M1")],
                     "continuation": {"mandate": "Prepare locally only.", "sources": ["fixture://user/mandate"],
                                      "constraints": ["T4 hold awaits Product decision."],
                                      "read_next": ["tracker:/missions/0"]}}
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def mission(self, mid, status="to-prepare"):
        return {"id": mid, "title": mid + " title", "ticket": "fixture://tickets/" + mid,
                "status": status, "criteria": [{"text": "Current acceptance", "result": "unverified"}],
                "depends_on": [], "owner": "", "blocker": "", "next_action": "Reconcile."}

    def write(self):
        self.html.write_text(self.tracker.render(self.template, self.data))
        for path, content in self.tracker.validate(self.data, self.root).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(self.html), *args],
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout), result.stdout
        self.assertNotEqual(result.returncode, 0)
        return result

    def revision(self):
        return self.cli()[0]["revision_sha256"]

    def detail(self, pointer, **kwargs):
        args = ["--pointer", pointer, "--expected", self.revision()]
        for key, value in kwargs.items():
            args += ["--" + key.replace("_", "-"), str(value)]
        return self.cli(*args)

    def test_large_overview_scans_all_missions_and_prioritizes_old_alerts(self):
        self.data["missions"] = [self.mission("M" + str(i), "done") for i in range(1000)]
        for mission in self.data["missions"]:
            mission["criteria"][0].update(result="passed", proof="fixture://old/evidence")
        uncertain = self.data["missions"][997]
        uncertain["dispatch"] = {"request_id": "request-uncertain", "status": "unknown", "work_revision": "sha-new"}
        self.data["missions"][998]["attempts"] = {"F9": {"used": 2, "limit": 2, "next_owner": "reviewer"}}
        self.data["missions"][999]["hold"] = {"active": True, "owner": "Product", "release_condition": "Choose 30 or 90"}
        self.data["journal"] = ["Old note " + str(i) + ": " + "superseded detail " * 20 for i in range(5000)]
        self.write()
        self.assertGreater(len(self.html.read_bytes()), 1_000_000)
        result, raw = self.cli("--limit", "1")
        self.assertLessEqual(len(raw.encode()), 12000)
        self.assertEqual(result["history"]["journal_entries"], 5000)
        for flag in ("uncertain_delivery", "exhausted_budget", "recorded_hold"):
            self.assertEqual(result["recorded_flags"][flag], 1)
        self.assertEqual(result["missions"][0]["id"], "M997")
        self.assertEqual(result["page"]["total"], 3)
        self.assertIsNotNone(result["page"]["next_offset"])
        self.assertTrue(result["partial"])

    def test_missing_contract_and_unknown_history_do_not_become_permission_or_zero(self):
        del self.data["continuation"]
        mission = self.data["missions"][0]
        mission["status"] = "running"
        mission["attempts"] = {"finding": 2}
        mission["prompt"] = {"kind": "follow-up", "delivery": "sent", "file": "prompts/M1.md", "text": "Existing work."}
        self.write()
        result, _ = self.cli()
        self.assertEqual(result["continuation_status"], "missing")
        self.assertEqual(result["recorded_flags"]["execution_identity_missing"], 1)
        self.assertEqual(result["recorded_flags"]["attempt_limit_unknown"], 1)
        self.assertNotIn("eligible", result)
        self.data["continuation"] = {"mandate": "Incomplete"}
        self.write()
        self.assertEqual(self.cli()[0]["continuation_status"], "incomplete")

    def test_guard_contract_and_unreleased_token_remain_discoverable_after_context_loss(self):
        mission = self.data["missions"][0]
        mission["status"] = "done"
        mission["criteria"][0].update(result="passed", proof="fixture://proof")
        mission["dispatch"] = {"request_id":"saved-request", "claim_state":"held", "status":"completed"}
        mission["guard"] = {"sources":[{"id":"S" + str(i), "fingerprint":"a" * 64} for i in range(40)],
                            "proof_sources":{"S0":"a" * 64}}
        self.complete_continuation("verify");self.write()
        overview,_ = self.cli()
        self.assertEqual(overview["recorded_flags"]["unreleased_claim"],1)
        self.assertEqual(overview["missions"][0]["id"],"M1")
        self.assertIn("guard",overview["missions"][0])
        sources,_ = self.detail("/missions/0/guard/sources",limit=1)
        self.assertIn('S0',json.dumps(sources))

    def complete_continuation(self, phase="reconcile"):
        self.data["continuation"]["current_action"] = {"phase": phase, "mission_ids": ["M1"],
                            "next_step": "Look up saved request before any send.",
                            "sources": ["tracker:/missions/0/dispatch"]}
        self.data["continuation"]["reconciled"] = rv.continuation_fingerprints(self.data)

    def test_legacy_contract_requires_an_action_checkpoint_before_action(self):
        result, _ = self.cli()
        self.assertEqual(result["continuation_status"], "incomplete")
        self.assertIn("current_action", " ".join(result["continuation_recovery_work"]))
        self.assertEqual(self.cli("--require-continuation", "--expected", self.revision(), success=False).stdout, "")
        self.complete_continuation()
        self.write()
        result, _ = self.cli("--require-continuation", "--expected", self.revision())
        self.assertEqual(result["continuation_status"], "recorded")
        self.assertEqual(result["continuation_recovery_work"], [])

    def test_later_recorded_decision_requires_reconciliation_even_at_fresh_sha(self):
        self.complete_continuation("prepare")
        self.data["decisions"].append("Product hold M1 until explicit release.")
        self.write()
        result, _ = self.cli()
        self.assertEqual(result["continuation_status"], "stale")
        self.assertIn("decisions_sha256", " ".join(result["continuation_recovery_work"]))
        self.assertEqual(self.cli("--require-continuation", "--expected", self.revision(), success=False).stdout, "")
        self.data["continuation"]["constraints"] = [self.data["decisions"][-1]]
        self.data["missions"][0]["hold"] = {"active": True, "owner": "Product"}
        self.complete_continuation("await_decision")
        self.write()
        result, _ = self.cli("--require-continuation", "--expected", self.revision())
        self.assertEqual(result["continuation"]["current_action"]["phase"], "await_decision")
        self.assertEqual(result["recorded_flags"]["recorded_hold"], 1)

    def test_interruption_before_or_after_acceptance_preserves_pending_intent(self):
        self.complete_continuation("dispatch_pending")
        mission = self.data["missions"][0]
        mission["prompt"] = {"kind": "follow-up", "delivery": "unknown", "file": "prompts/M1.md",
                             "text": "$implement fixture://tickets/M1\nAddress F9."}
        mission["dispatch"] = {"delivery": "unknown", "request_id": "stable-request",
                               "payload_sha256": hashlib.sha256(mission["prompt"]["text"].encode()).hexdigest(),
                               "destination": "fixture://orchestrator", "work_revision": "revision-1"}
        mission["attempts"] = {"F9": {"used": 2, "limit": 2, "next_owner": "reviewer"}}
        self.complete_continuation("dispatch_pending")
        self.write()
        saved = self.tracker.revision(self.html)
        # Independent reads before the call and after acceptance with a lost receipt
        # see identical durable bytes; neither may infer a new dispatch slot.
        before, _ = self.cli("--require-continuation", "--expected", self.revision())
        after, _ = self.cli("--require-continuation", "--expected", self.revision())
        self.assertEqual(before, after)
        self.assertEqual(after["recorded_flags"]["uncertain_delivery"], 1)
        self.assertEqual(after["recorded_flags"]["exhausted_budget"], 1)
        self.assertEqual(after["continuation"]["current_action"]["phase"], "dispatch_pending")
        self.assertEqual(saved, self.tracker.revision(self.html))
        # A lookup, rather than a second send, supplies the original receipt.
        mission["dispatch"].update(delivery="accepted", task_id="actual-task")
        mission["prompt"]["delivery"] = "sent"
        mission["status"] = "running"
        self.complete_continuation("await_result")
        self.write()
        reconciled, _ = self.cli("--require-continuation", "--expected", self.revision())
        self.assertNotIn("uncertain_delivery", reconciled["recorded_flags"])
        self.assertEqual(mission["attempts"]["F9"]["used"], 2)

    def test_checkpoint_rejects_unknown_mission_and_invalid_phase_or_cursor(self):
        self.complete_continuation()
        for key, value in (("phase", "accepted"), ("mission_ids", ["missing"]), ("sources", [])):
            original = self.data["continuation"]["current_action"][key]
            self.data["continuation"]["current_action"][key] = value
            self.write()
            self.assertEqual(self.cli()[0]["continuation_status"], "incomplete")
            self.data["continuation"]["current_action"][key] = original
        self.data["continuation"]["reconciled"]["journal_sha256"] = True
        self.write()
        self.assertEqual(self.cli()[0]["continuation_status"], "incomplete")

    def test_rewritten_recorded_decision_is_detected_without_a_length_change(self):
        self.data["decisions"] = ["Prepare M1"]
        self.complete_continuation()
        self.data["decisions"][0] = "Hold M1 until Product release"
        self.write()
        self.assertEqual(self.cli()[0]["continuation_status"], "stale")

    def test_mission_only_changes_invalidate_checkpoint_even_without_new_history(self):
        self.complete_continuation("prepare")
        for field, value in (("hold", {"active": True, "owner": "Product"}),
                             ("attempts", {"F9": {"used": 2, "limit": 2}}),
                             ("dispatch", {"request_id": "new-observation", "delivery": "unknown"}),
                             ("work_revision", "new-head")):
            with self.subTest(field=field):
                self.data["missions"][0][field] = value
                self.write()
                self.assertEqual(self.cli()[0]["continuation_status"], "stale")
                rejected = self.cli("--require-continuation", "--expected", self.revision(), success=False)
                self.assertIn("state_sha256", rejected.stderr)
                self.data["missions"][0].pop(field)

    def test_mandate_and_source_edits_need_a_new_reconciled_checkpoint(self):
        self.complete_continuation()
        for target, key, value in ((self.data, "spec", "fixture://spec/changed"),
                                   (self.data, "next_action", "Wait for explicit approval"),
                                   (self.data["continuation"], "mandate", "Prepare only; no sends"),
                                   (self.data["continuation"]["current_action"], "phase", "await_decision")):
            with self.subTest(key=key):
                original = target[key]
                target[key] = value
                self.write()
                self.assertEqual(self.cli()[0]["continuation_status"], "stale")
                target[key] = original

    def test_save_timestamp_and_editing_metadata_do_not_invalidate_state_binding(self):
        self.complete_continuation()
        self.data["updated_at"] = "Later save timestamp"
        self.data["_base_sha256"] = "0" * 64
        # Temporary editing metadata is excluded from the saved-state binding.
        self.assertEqual(rv.continuation_review(self.data)[0], "recorded")
        self.data.pop("_base_sha256")
        self.write()
        self.assertEqual(self.cli()[0]["continuation_status"], "recorded")

    def test_prior_history_only_checkpoint_remains_readable_but_needs_reconciliation(self):
        self.complete_continuation()
        self.data["continuation"]["reconciled"].pop("state_sha256")
        self.write()
        result, _ = self.cli()
        self.assertEqual(result["continuation_status"], "incomplete")
        self.assertIn("history/state", " ".join(result["continuation_recovery_work"]))
        self.complete_continuation()
        self.write()
        self.cli("--require-continuation", "--expected", self.revision())

    def test_completed_history_stays_quiet_but_uncertainty_is_not_hidden_by_done(self):
        mission = self.data["missions"][0]
        mission["status"] = "done"
        mission["criteria"][0].update(result="passed", proof="fixture://received")
        mission["prompt"] = {"kind": "implementation", "delivery": "sent", "file": "prompts/M1.md", "text": "Historical send."}
        self.write()
        result, _ = self.cli()
        self.assertEqual(result["page"]["total"], 0)
        mission["prompt"]["delivery"] = "unknown"
        self.write()
        result, _ = self.cli()
        self.assertEqual(result["recorded_flags"]["uncertain_delivery"], 1)
        self.assertEqual(result["recorded_flags"]["execution_identity_missing"], 1)
        self.assertEqual(result["page"]["total"], 1)

    def test_selected_reads_require_revision_and_reject_changes_between_pages(self):
        self.data["missions"].append(self.mission("M2"))
        self.write()
        sha = self.revision()
        self.assertIn("require --expected", self.cli("--mission", "M1", success=False).stderr)
        self.assertIn("require --expected", self.cli("--offset", "1", success=False).stderr)
        following, _ = self.cli("--offset", "1", "--expected", sha)
        self.assertEqual(following["missions"][0]["id"], "M2")
        result, _ = self.cli("--mission", "M1", "--expected", sha)
        self.assertEqual(result["pointer"], "/missions/0")
        self.html.write_bytes(self.html.read_bytes() + b"\n<!-- changed -->\n")
        for args in (("--pointer", "/missions/0"), ("--offset", "0")):
            rejected = self.cli(*args, "--expected", sha, success=False)
            self.assertIn("Stale tracker revision", rejected.stderr)
            self.assertEqual(rejected.stdout, "")

    def test_exact_unicode_prompt_chunks_reconstruct_without_loss_under_byte_limit(self):
        text = "Ligne é😀 / value\n" * 250
        self.data["missions"][0]["prompt"] = {"kind": "implementation", "delivery": "prepared",
                                            "file": "prompts/M1.md", "text": text}
        self.write()
        sha = self.revision()
        offset, pieces = 0, []
        while True:
            result, raw = self.cli("--pointer", "/missions/0/prompt/text", "--expected", sha,
                                   "--offset", str(offset), "--limit", "10000", "--max-bytes", "2048")
            self.assertLessEqual(len(raw.encode()), 2048)
            pieces.append(result["text"])
            if result["next_offset"] is None:
                break
            self.assertGreater(result["next_offset"], offset)
            offset = result["next_offset"]
        self.assertEqual("".join(pieces), text)
        self.assertTrue(result["partial"])

    def test_tight_utf8_envelope_never_returns_a_nonprogress_cursor(self):
        key = "x" * 1668
        self.data[key] = "😀"
        self.write()
        result, raw = self.cli("--pointer", "/" + key, "--expected", self.revision(),
                               "--limit", "1", "--max-bytes", "2048")
        self.assertLessEqual(len(raw.encode("utf-8")), 2048)
        self.assertTrue(result.get("budget_exceeded") or result.get("returned", 0) > 0)
        self.assertNotEqual(result.get("next_offset"), 0)

    def test_long_valid_dependency_chain_is_readable_in_both_mission_orders(self):
        self.data["missions"] = [self.mission("M" + str(i)) for i in range(1006)]
        for i, mission in enumerate(self.data["missions"][:-1]):
            mission["depends_on"] = ["M" + str(i + 1)]
        # render does not validate; public CLI reads must validate this deep DAG.
        for missions in (self.data["missions"], list(reversed(self.data["missions"]))):
            self.data["missions"] = missions
            self.html.write_text(self.tracker.render(self.template, self.data))
            result, _ = self.cli("--limit", "1")
            self.assertEqual(result["mission_counts"]["to-prepare"], 1006)
            self.cli("--pointer", "/journal", "--expected", result["revision_sha256"])

    def test_list_search_preserves_original_pointers_and_pages_are_complete(self):
        self.data["journal"] = ["ordinary"] * 30
        self.data["journal"][0] = "request[7] early accepted"
        self.data["journal"][29] = "request[7] same request, not a new send"
        self.data["decisions"] = ["Decision " + str(i) for i in range(25)]
        self.data["custom"] = {"a/b~c": "Exact escaped key"}
        self.write()
        result, _ = self.detail("/journal", contains="request[7]", limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["pointer"], "/journal/0")
        result, _ = self.detail("/journal", contains="request[7]", offset=1, limit=1)
        self.assertEqual(result["items"][0]["pointer"], "/journal/29")
        self.assertIsNone(result["next_offset"])
        result, _ = self.detail("/custom/a~1b~0c")
        self.assertEqual(result["text"], "Exact escaped key")
        values, offset = [], 0
        while True:
            result, _ = self.detail("/decisions", offset=offset, limit=7)
            values += [item["value"] for item in result["items"]]
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
        self.assertEqual(values, self.data["decisions"])

    def test_oversized_contract_discloses_clipping_or_budget_failure(self):
        self.data["continuation"]["mandate"] = "😀" * 10000
        self.data["continuation"]["constraints"] = ["😀" * 1000 for _ in range(100)]
        self.write()
        result, raw = self.cli("--max-bytes", "2048")
        self.assertLessEqual(len(raw.encode()), 2048)
        self.assertTrue(result.get("budget_exceeded") or result["continuation_requires_expansion"])
        result, _ = self.detail("/continuation/mandate", max_bytes=2048)
        self.assertTrue(result["text"])
        self.assertIsNotNone(result["next_offset"])

    def test_views_are_read_only_and_cannot_be_saved_as_canonical_state(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result, raw = self.cli()
        self.assertTrue(result["view_only"])
        self.assertNotIn("_base_sha256", result)
        self.detail("/missions/0")
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        with self.assertRaisesRegex(ValueError, "base revision"):
            self.tracker.save_state(self.html, result)

    def test_invalid_pointers_and_ranges_fail_without_exposing_selected_data(self):
        sha = self.revision()
        for pointer in ("/missions/-1", "/missions/00", "/missions/10", "/missing", "/custom~2key"):
            with self.subTest(pointer=pointer):
                result = self.cli("--pointer", pointer, "--expected", sha, success=False)
                self.assertEqual(result.stdout, "")
        self.cli("--offset", "-1", success=False)
        self.cli("--max-bytes", "1", success=False)

    def test_new_checkpoints_bundle_the_read_only_helper_and_preserve_capsule(self):
        self.complete_continuation()
        self.write()
        files = cp.make_snapshot(self.root, "tracker.html", [])
        self.assertEqual(files["tools/resume.py"], SCRIPT.read_bytes())
        self.assertEqual(files["tools/bootstrap.py"], SCRIPT.with_name("bootstrap.py").read_bytes())
        for name in ("guard.py", "claims.py", "sources.py"):
            self.assertEqual(files["tools/" + name], SCRIPT.with_name(name).read_bytes())
        cp.verify_files(files)
        restored = self.root / "new-root"
        cp.restore(files, restored)
        helper = restored / ".checkpoint/tools/resume.py"
        result = subprocess.run([sys.executable, "-B", str(helper), str(restored / "tracker.html")],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["continuation_status"], "recorded")
        for name in ("guard.py", "claims.py", "sources.py"):
            result = subprocess.run([sys.executable, "-B", str(helper.with_name(name)), "--help"],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
