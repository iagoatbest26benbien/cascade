"""Real LOCAL bare-Git claims, including independent process contention."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "claims.py"
spec = importlib.util.spec_from_file_location("claims", SCRIPT)
claims = importlib.util.module_from_spec(spec)
spec.loader.exec_module(claims)
TICKET = "https://tickets.example.test/Team/Issue/123"
PAYLOAD = hashlib.sha256(b"synthetic authorized assignment").hexdigest()
PROOF = {"outcome": "not_delivered", "source": "fixture://observations/confirmed-rejection"}


def git(*args):
    result = subprocess.run(["git", *map(str, args)], check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=15)
    return result.stdout.decode().strip()


# Each worker starts in its own actual clone. The barrier is immediately before
# the actual push: all processes have independently observed the same old value.
WORKER = r'''
import importlib.util, json, os, pathlib, sys, time
script, remote, ticket, request, owner, payload, barrier, index, count = sys.argv[1:]
spec = importlib.util.spec_from_file_location("worker_claims", script)
claims = importlib.util.module_from_spec(spec)
spec.loader.exec_module(claims)
original = claims._push
def push(*args):
    root = pathlib.Path(barrier)
    (root / index).write_text(str(os.getpid()))
    deadline = time.monotonic() + 12
    while len(list(root.iterdir())) < int(count):
        if time.monotonic() > deadline:
            raise ValueError("process barrier timed out")
        time.sleep(.01)
    return original(*args)
claims._push = push
try:
    result = claims.acquire(remote, ticket, request, owner, payload)
except ValueError as error:
    print(json.dumps({"ok": False, "reason": str(error)}))
else:
    print(json.dumps({"ok": True, "claim": result}))
'''


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claims-tests-")
        self.base = Path(self.temporary.name)
        self.remote = self.base / "LOCAL authority.git"
        git("init", "--quiet", "--bare", "--template=", self.remote)
        self.remote_arg = str(self.remote)
        self.ref = "refs/heads/claims/" + hashlib.sha256(TICKET.encode()).hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def acquire(self, request="request-1", owner="workstream/A", ticket=TICKET):
        return claims.acquire(self.remote_arg, ticket, request, owner, PAYLOAD)

    def release(self, held, proof=PROOF):
        return claims.release(self.remote_arg, TICKET, held["request_id"], held["claim_commit"], proof)

    def head(self):
        return git("--git-dir", self.remote, "rev-parse", self.ref)

    def replace_record(self, record, parent):
        """Authority-side corruption fixture; never calls a production remote."""
        with tempfile.TemporaryDirectory(dir=self.base) as directory:
            repo = Path(directory)
            claims._init(repo)
            claims._git(repo, "fetch", "--quiet", self.remote_arg, self.ref)
            commit = claims._commit(repo, record, parent)
            claims._git(repo, "push", "--quiet", "--force", self.remote_arg, f"{commit}:{self.ref}")
        return commit

    def record_only(self, claim):
        return {k: v for k, v in claim.items() if k not in {"claim_commit", "ref"}}

    def contention(self, identical=False, released=False):
        prior = None
        if released:
            prior = self.release(self.acquire("original-request"))
        barrier = self.base / "barrier"
        barrier.mkdir()
        workers = []
        try:
            for i in range(4):
                clone = self.base / f"independent-clone-{i}"
                git("clone", "--quiet", "--no-checkout", self.remote, clone)
                request = "same-request" if identical else f"independent-request-{i}"
                owner = "same-owner" if identical else f"workstream/{i}"
                workers.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", WORKER, str(SCRIPT), self.remote_arg,
                     TICKET, request, owner, PAYLOAD, str(barrier), str(i), "4"],
                    cwd=clone, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            results = []
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=25)
                self.assertEqual(worker.returncode, 0, stderr)
                results.append(json.loads(stdout))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()
        self.assertEqual(len(list(barrier.iterdir())), 4, "all independent snapshots must contend at push")
        self.assertEqual(sum(r["ok"] for r in results), 1, results)
        winner = next(r["claim"] for r in results if r["ok"])
        self.assertEqual(claims.inspect(self.remote_arg, TICKET), winner)
        if prior:
            self.assertEqual(git("--git-dir", self.remote, "rev-parse", winner["claim_commit"] + "^"), prior["claim_commit"])
        else:
            self.assertEqual(git("--git-dir", self.remote, "rev-list", "--count", self.ref), "1")

    def test_simultaneous_independent_processes_only_one_acquires(self):
        self.contention()

    def test_simultaneous_identical_identity_cannot_both_get_permit(self):
        self.contention(identical=True)

    def test_simultaneous_reacquire_of_released_claim_only_one_succeeds(self):
        self.contention(released=True)

    def test_round_trip_and_released_retry_preserve_provenance(self):
        self.assertIsNone(claims.inspect(self.remote_arg, TICKET))
        held = self.acquire()
        self.assertEqual(held["ref"], self.ref)
        self.assertEqual(held["phase"], "held")
        self.assertEqual(claims.inspect(self.remote_arg, TICKET), held)
        released = self.release(held)
        self.assertEqual(released["phase"], "released")
        self.assertEqual(released["proof"], PROOF)
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", released["claim_commit"] + "^"), held["claim_commit"])
        next_claim = self.acquire("request-2", "workstream/B")
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", next_claim["claim_commit"] + "^"), released["claim_commit"])
        self.assertEqual(git("--git-dir", self.remote, "show", "-s", "--format=%an <%ae>", self.ref), claims.AUTHOR)
        self.assertNotIn(self.remote_arg, json.dumps(next_claim))

    def test_terminal_release_is_allowed(self):
        held = self.acquire()
        proof = {"outcome": "terminal", "source": "fixture://observations/worker-finished"}
        self.assertEqual(self.release(held, proof)["proof"], proof)

    def test_all_existing_held_claims_block_even_same_identity_or_wrong_owner(self):
        held = self.acquire()
        for request, owner in (("request-1", "workstream/A"), ("request-1", "wrong-owner"), ("request-2", "workstream/B")):
            with self.subTest(request=request, owner=owner), self.assertRaisesRegex(ValueError, "held claim"):
                self.acquire(request, owner)
        self.assertEqual(self.head(), held["claim_commit"])

    def test_released_request_cannot_be_reused_even_after_another_request(self):
        self.release(self.acquire())
        with self.assertRaisesRegex(ValueError, "request ID"):
            self.acquire()
        self.release(self.acquire("request-2"))
        with self.assertRaisesRegex(ValueError, "request ID"):
            self.acquire("request-1", "other-owner")

    def test_release_requires_exact_request_and_current_held_commit(self):
        held = self.acquire()
        for request, commit in (("wrong-request", held["claim_commit"]), (held["request_id"], "0" * 40)):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, "does not match"):
                claims.release(self.remote_arg, TICKET, request, commit, PROOF)
        self.release(held)
        newer = self.acquire("request-2", "new-owner")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.release(held)
        self.assertEqual(self.head(), newer["claim_commit"])

    def test_timeout_unknown_and_malformed_proofs_never_release(self):
        held = self.acquire()
        for proof in ({}, {"outcome": "timeout", "source": "fixture://timeout"},
                      {"outcome": "unknown", "source": "fixture://unknown"},
                      {"outcome": [], "source": "fixture://invalid"},
                      {"outcome": "terminal", "source": " "},
                      dict(PROOF, owner="forged"), {"outcome": "terminal", "source": "x\nsecret"}):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                self.release(held, proof)
        self.assertEqual(self.head(), held["claim_commit"])

    def test_ticket_normalizes_scheme_host_but_preserves_path_case(self):
        held = self.acquire(ticket="HTTPS://TICKETS.EXAMPLE.TEST/Team/Issue/123")
        self.assertEqual(held["ticket"], TICKET)
        self.assertEqual(held["ref"], self.ref)
        with self.assertRaises(ValueError):
            self.acquire(ticket=TICKET)
        different = self.acquire("case-distinct", ticket=TICKET.replace("Team", "team"))
        self.assertNotEqual(held["ref"], different["ref"])

    def test_bad_inputs_are_rejected_before_any_git_call(self):
        with patch.object(claims, "_git", side_effect=AssertionError("must not touch transport")):
            for ticket in ("https://user:secret@host.test/issues/1", TICKET + "?token=secret", TICKET + "#fragment",
                           TICKET + "?", TICKET + "#", "https://host.test", "file:///tmp/ticket", "https://host.test/x\n"):
                with self.subTest(ticket=ticket), self.assertRaises(ValueError):
                    self.acquire(ticket=ticket)
            for remote in ("https://user:secret@host.test/repo", "ext::bad", "relative.git", "https://host.test/repo?token=secret"):
                with self.subTest(remote=remote), self.assertRaises(ValueError):
                    claims.inspect(remote, TICKET)
            for request, owner, payload in (("", "owner", PAYLOAD), ("request", "", PAYLOAD), ("request", "owner", "A" * 64)):
                with self.subTest(request=request), self.assertRaises(ValueError):
                    claims.acquire(self.remote_arg, TICKET, request, owner, payload)

    def test_push_accepted_but_response_lost_is_not_success_or_retry_permit(self):
        original = claims._push
        def lost_response(*args):
            original(*args)
            raise ValueError("simulated response lost after acceptance")
        with patch.object(claims, "_push", side_effect=lost_response), self.assertRaises(ValueError):
            self.acquire()
        observed = claims.inspect(self.remote_arg, TICKET)
        self.assertEqual(observed["phase"], "held")
        with self.assertRaisesRegex(ValueError, "held claim"):
            self.acquire()
        self.release(observed)

    def test_unknown_push_without_delivery_stays_an_error(self):
        with patch.object(claims, "_push", side_effect=ValueError("simulated unknown transport")):
            with self.assertRaises(ValueError):
                self.acquire()
        self.assertIsNone(claims.inspect(self.remote_arg, TICKET))

    def test_successful_push_but_failed_fresh_read_is_not_success(self):
        with patch.object(claims, "inspect", side_effect=ValueError("simulated read failure")):
            with self.assertRaisesRegex(ValueError, "fresh confirmation failed"):
                self.acquire()
        self.assertEqual(claims.inspect(self.remote_arg, TICKET)["phase"], "held")
        with self.assertRaises(ValueError):
            self.acquire()

    def test_stale_lease_does_not_overwrite_competing_claim(self):
        original = claims._push
        winners = []
        def raced(repo, remote, ref, parent, commit):
            with patch.object(claims, "_push", original):
                winners.append(self.acquire("competing-request", "other-workstream"))
            return original(repo, remote, ref, parent, commit)
        with patch.object(claims, "_push", side_effect=raced), self.assertRaises(ValueError):
            self.acquire()
        self.assertEqual(self.head(), winners[0]["claim_commit"])

    def test_stale_release_lease_does_not_overwrite_new_owner(self):
        held = self.acquire()
        original = claims._push
        newer = []
        def raced(repo, remote, ref, parent, commit):
            with patch.object(claims, "_push", original):
                self.release(held)
                newer.append(self.acquire("new-request", "new-owner"))
            return original(repo, remote, ref, parent, commit)
        with patch.object(claims, "_push", side_effect=raced), self.assertRaises(ValueError):
            self.release(held)
        self.assertEqual(self.head(), newer[0]["claim_commit"])

    def test_deleted_or_rewound_ref_after_push_fails_fresh_confirmation(self):
        held = self.acquire()
        released = self.release(held)
        original = claims._push
        def rewound(*args):
            original(*args)
            git("--git-dir", self.remote, "update-ref", self.ref, released["claim_commit"])
        with patch.object(claims, "_push", side_effect=rewound):
            with self.assertRaisesRegex(ValueError, "fresh confirmation failed"):
                self.acquire("new-request")
        def deleted(*args):
            original(*args)
            git("--git-dir", self.remote, "update-ref", "-d", self.ref)
        with patch.object(claims, "_push", side_effect=deleted):
            with self.assertRaisesRegex(ValueError, "fresh confirmation failed"):
                self.acquire("another-request")
        with self.assertRaises(ValueError):
            self.release(held)

    def test_changed_ref_during_fetch_blocks_inspect(self):
        held = self.acquire()
        released = self.release(held)
        original = claims._git
        def changed(repo, *args, **kwargs):
            if args[0] == "fetch":
                git("--git-dir", self.remote, "update-ref", self.ref, held["claim_commit"])
            return original(repo, *args, **kwargs)
        with patch.object(claims, "_git", side_effect=changed), self.assertRaisesRegex(ValueError, "changed during fetch"):
            claims.inspect(self.remote_arg, TICKET)
        self.assertNotEqual(self.head(), released["claim_commit"])

    def test_modified_owner_in_release_record_is_rejected(self):
        held = self.acquire()
        forged = dict(self.record_only(held), phase="released", owner="forged-owner", proof=PROOF)
        self.replace_record(forged, held["claim_commit"])
        for operation in (lambda: claims.inspect(self.remote_arg, TICKET), lambda: self.acquire("next-request"), lambda: self.release(held)):
            with self.assertRaisesRegex(ValueError, "identity"):
                operation()

    def test_held_overwrite_without_release_and_wrong_ticket_are_rejected(self):
        held = self.acquire()
        forged = dict(self.record_only(held), request_id="overwrite", owner="other-owner")
        self.replace_record(forged, held["claim_commit"])
        with self.assertRaisesRegex(ValueError, "without release"):
            claims.inspect(self.remote_arg, TICKET)
        forged["ticket"] = "https://tickets.example.test/Other/1"
        self.replace_record(forged, held["claim_commit"])
        with self.assertRaisesRegex(ValueError, "identity"):
            claims.inspect(self.remote_arg, TICKET)

    def test_bounded_history_and_records_fail_closed(self):
        held = self.acquire()
        with patch.object(claims, "MAX_HISTORY", 0), self.assertRaisesRegex(ValueError, "bound"):
            claims.inspect(self.remote_arg, TICKET)
        with patch.object(claims, "MAX_RECORD_BYTES", 8), self.assertRaisesRegex(ValueError, "bound"):
            claims.inspect(self.remote_arg, TICKET)
        self.assertEqual(self.head(), held["claim_commit"])

    def test_duplicate_json_fields_and_invalid_proof_record_fail_closed(self):
        held = self.acquire()
        original = claims._git
        valid = json.dumps(self.record_only(held))
        malformed = valid[:-1] + ', "owner": "forged"}'
        for data in (malformed.encode(), b'[]', b'{broken', b'[' * 1500):
            def corrupted(repo, *args, **kwargs):
                if args[:2] == ("cat-file", "blob"):
                    return data
                return original(repo, *args, **kwargs)
            with self.subTest(data=data[:20]), patch.object(claims, "_git", side_effect=corrupted):
                with self.assertRaises(ValueError):
                    claims.inspect(self.remote_arg, TICKET)
        self.assertEqual(self.head(), held["claim_commit"])

    def test_remote_unavailable_is_not_an_absent_claim(self):
        with self.assertRaises(ValueError):
            claims.inspect(str(self.base / "missing-LOCAL-authority.git"), TICKET)

    def test_local_authority_must_be_bare(self):
        checkout = self.base / "non-bare-checkout"
        git("init", "--quiet", "--template=", checkout)
        with self.assertRaises(ValueError):
            claims.acquire(str(checkout), TICKET, "request", "owner", PAYLOAD)
        self.assertEqual(git("-C", checkout, "for-each-ref"), "")

    def test_release_response_loss_is_not_success_and_stale_repeat_blocks(self):
        held = self.acquire()
        original = claims._push
        def lost_response(*args):
            original(*args)
            raise ValueError("release response lost")
        with patch.object(claims, "_push", side_effect=lost_response), self.assertRaises(ValueError):
            self.release(held)
        observed = claims.inspect(self.remote_arg, TICKET)
        self.assertEqual(observed["phase"], "released")
        self.assertEqual(observed["owner"], held["owner"])
        with self.assertRaises(ValueError):
            self.release(held)
        self.assertEqual(self.acquire("new-request")["phase"], "held")

    def test_subprocess_timeout_and_errors_do_not_expose_auth(self):
        secret = "do-not-echo-private-auth"
        with patch.object(claims.subprocess, "run", side_effect=subprocess.TimeoutExpired(["git", secret], 30)):
            with self.assertRaises(ValueError) as error:
                claims.inspect(self.remote_arg, TICKET)
        self.assertNotIn(secret, str(error.exception))
        result = subprocess.CompletedProcess(["git"], 1, b"", secret.encode())
        with patch.object(claims.subprocess, "run", return_value=result):
            with self.assertRaises(ValueError) as error:
                claims.inspect(self.remote_arg, TICKET)
        self.assertNotIn(secret, str(error.exception))

    def test_cli_round_trip_and_help(self):
        def cli(*args):
            return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        self.assertEqual(cli("--help").returncode, 0)
        args = ["--remote", self.remote_arg, "--ticket", TICKET]
        initial = cli("inspect", *args)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertIsNone(json.loads(initial.stdout))
        result = cli("acquire", *args, "--request-id", "cli-request", "--owner", "cli-owner", "--payload-sha256", PAYLOAD)
        self.assertEqual(result.returncode, 0, result.stderr)
        held = json.loads(result.stdout)
        duplicate = cli("acquire", *args, "--request-id", "cli-request", "--owner", "cli-owner", "--payload-sha256", PAYLOAD)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(duplicate.stdout, "")
        released = cli("release", *args, "--request-id", "cli-request", "--claim-commit", held["claim_commit"], "--proof", json.dumps(PROOF))
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(json.loads(released.stdout)["phase"], "released")


if __name__ == "__main__":
    unittest.main()
