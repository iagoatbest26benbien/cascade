# Dispatch guards and evidence freshness

Read before authorized automatic/delegated dispatch, competing coordinators, or acceptance based on earlier evidence. For the supplied tracker, use `guard.py` for every outbound assignment and its acceptance check. Manual prompt handoffs need no reservation. For another tracker format, preserve its reader and apply equivalent blocking checks, write-ahead intent, atomic service claim and fresh proof bindings; serialize dispatch until those equivalents exist. Reading a legacy tracker remains supported.

These helpers implement the coordination workflow; they do not intercept arbitrary native/MCP calls. A green guard establishes recorded eligibility, not user authorization, semantic acceptance or exactly-once execution by an external service. The coordinator must translate the actual mandate, scoped holds and observed receipts faithfully. Resolve unstructured constraints before encoding the contract.

## Establish the contract in the HTML

Record `mission.guard` through the tracker's revision-checked save, alongside the complete continuation and prepared prompt:

```json
{
  "authorization_source": "ACTUAL_USER_MANDATE_LOCATOR",
  "claim_remote": "https://github.com/ORG/PRIVATE-COORDINATION.git",
  "tool": "EXACT_DISPATCH_TOOL_NAME",
  "prompt_pointer": "/task",
  "request_id_pointer": "/clientRequestId",
  "sources": [
    {"id":"ticket","role":"ticket","kind":"github_issue","repository":"ORG/REPO","number":123,"fingerprint":"CAPTURED_SHA256"},
    {"id":"code","role":"code","kind":"git","path":"/absolute/checkout","ref":"HEAD","fingerprint":"CAPTURED_SHA256"},
    {"id":"ci","role":"ci","kind":"github_checks","repository":"ORG/REPO","ref":"ACTUAL_HEAD_SHA","fingerprint":"CAPTURED_SHA256"}
  ]
}
```

Use actual values, never the placeholders above. `authorization_source` must also appear in `continuation.sources`. Configure the JSON pointers for the actual tool's arguments; a service without a client key uses `request_id_pointer: null` while retaining a stable internal request ID. The exact payload envelope is `{"tool":"NAME","arguments":{...}}`; its selected prompt must equal the prepared prompt text. Include the intended destination, provider/model/options and work revision in those arguments and the mission's source/work metadata as applicable. Check the service's actual receipt separately.

Capture expected versions when reconciling requirements and preparing work:

```bash
python -B <tools>/sources.py capture --root /absolute/workstream \
  --spec '{"id":"ticket","role":"ticket","kind":"github_issue","repository":"ORG/REPO","number":123}'
```

In an installed skill, `<tools>` is its absolute `scripts/` directory; in the shared clone or restored checkpoint it is `tools/`. Save the returned descriptor into the HTML; stdout is not another maintained register. `evaluate` and CLI `check --spec JSON` reread without replacing expectations. Every source needs a unique ID, a role and a fingerprint. Cover `ticket`, `code`, `ci`; a genuinely irrelevant role needs `guard.not_applicable: {"ci":"Concrete reason"}`. At least one real source is required. A local file snapshot is fresh only relative to that file; use the authoritative live ticket adapter when external ticket changes matter.

| Kind | Descriptor fields | Observation |
| --- | --- | --- |
| `file` | `path`, absolute or relative to tracker directory | Regular file bytes, including changes with unchanged mtime |
| `git` | `path`, `ref` | Resolved commit; literal `HEAD` additionally requires a clean worktree, including untracked files/submodules |
| `github_issue` | `repository`, `number` | Requirements, state, labels, milestone and update version |
| `github_checks` | `repository`, `ref` | Resolved SHA, paginated suites/runs and classic statuses, with head recheck |

GitHub adapters use authenticated `gh api` GET on github.com. Missing tools/access, partial pages, moving heads, malformed responses and finite read limits produce `unknown`, which blocks. Equality never establishes a passing CI result or exact Actions producer-run provenance; verify those requirements from actual evidence. Reads are observations; external changes after a completed gate remain possible. Reobserve at each decision. Detailed limits are constants in `sources.py`; there is no silent truncation or TTL-based green result.

For a local Git checkout recovered from a separate repository, add `recovery: {"remote":"CLEAN_GIT_REMOTE","commit":"FULL_COMMIT"}` to that source descriptor. The commit must match its expected Git fingerprint. Checkpoint creation freshly fetches that exact commit into a disposable repository and records the recovery inventory and observation time; it never includes `.git` or copies the code repository into the checkpoint. Missing/unretrievable/mismatched locators block publication. Verification checks the pinned inventory; it does not promise permanent remote retention. After restore, rehydrate the declared checkout from that remote at the exact commit, verify it, and update only its host path if needed. Until then the source remains unknown and dispatch/acceptance blocks. Preserve proof bindings; rehydration does not refresh old evidence. Without this explicit contract, the normal uncaptured-dependency check still applies. Use off-host remotes for host-loss recovery; local bare endpoints are test fixtures.

**Done when:** the saved contract identifies the real authority and required source versions; the coordinator has reconciled constraints and source changes, and the continuation fingerprints match the saved state.

## Check and reserve one authorized call

Get the current HTML SHA from `resume.py`, expand relevant pointers, and require its reconciled continuation. Then:

```bash
python -B <tools>/guard.py check /absolute/workstream/tracker.html \
  --mission M1 --expected HTML_SHA256
python -B <tools>/guard.py prepare /absolute/workstream/tracker.html \
  --mission M1 --expected HTML_SHA256 --request-id STABLE_REQUEST_ID \
  --owner COORDINATOR_ID --payload /absolute/exact-tool-envelope.json
```

For a correction, pass `--finding STABLE_FINDING_ID` to both commands. Its `mission.attempts[ID]` needs integer `used`, positive `limit`, and the actual cumulative history. The optional `automatic_correction_budget` is cumulative too. An unresolved exhausted/malformed finding, global/mission hold, blocker, unmet/unreceived dependency, stale continuation, uncertain/active previous dispatch or changed/unknown source blocks preparation. Missing structured metadata never becomes permission or zero attempts.

A selected finding marked resolved must first be explicitly reconciled/reopened while retaining its cumulative counter; selecting that old entry cannot bypass its limit. Literal Git `HEAD` sources reject index flags and reduced-stat configurations that suppress dirty-file checks; the check disables filesystem-monitor shortcuts without modifying the caller's index/configuration. Explicit pinned refs remain observations of committed revision.

HEAD checks also inspect initialized nested submodules within the same observation budget, including their index flags/configuration and indexed commit identities. Unavailable/uninitialized worktrees or an exhausted budget return unknown; the observer never initializes modules. Rehydrate required modules explicitly after a restore.

`prepare` archives exact envelope bytes under `evidence/dispatch/<SHA>.json`, saves delivery `unknown` and reserves correction budget **before** acquiring the shared claim. It rechecks tracker revision, payload bytes and live sources after acquisition. Only exit 0 with `ready_for_authorized_call: true` permits **one already-authorized call using that archived envelope**. The helper sends nothing. Plain `check`, `recover`, a previous success report, or a repeated `prepare` grants no call permit. A failure after saving intent stays pending for reconciliation. Do not retry preparation with another key to escape uncertainty.

When host-loss recovery is required, publish/verify the exact pending intent and restore prerequisites before calling, following [durable checkpoints](durable-checkpoints.md). Keep the claim held throughout that boundary. If a delay or publication occurs after preparation, reread authority and sources before using its permit; a changed condition cancels sending and requires reconciliation.

**Done when:** one current preparation succeeded, the claim is held, all applicable recovery gates passed, and the exact authorized outbound call is ready. Record the actual response immediately after the call.

## Record observations and recover uncertainty

Use `guard.py record HTML --mission M1 --expected CURRENT_SHA --request-id KEY --receipt /absolute/receipt.json`. Every receipt contains matching `request_id`, an actual observation `source`, and one outcome:

- `accepted`: actual `task_id` or `run_id`, status `queued` or `running`.
- `terminal`: actual matching task/run ID, status `completed`, `failed` or `cancelled`, and `no_pending_work: true` verified from the service, including nested/queued work. This also records acceptance when the first response is already terminal.
- `not_delivered`: authoritative confirmation that this unknown request was not accepted. A timeout, missing local receipt or unsuccessful search alone is inconclusive. The helper can refund the reserved correction attempt only if its counters still match the reservation exactly.

Once an execution ID is known, subsequent accepted/terminal receipts must match at least one known task/run ID and cannot contradict any supplied known ID. Adding another kind of identifier alone does not establish that it belongs to the original execution.

The helper checks structure and identity; the coordinator supplies factual observations. Store raw returned provider/model/options and task details as evidence when applicable. Terminal work becomes `to-verify`; its criteria remain unaccepted. To conclude an owned claim, use `guard.py release HTML --mission M1 --expected CURRENT_SHA --request-id KEY` only after the conclusive receipt. Unknown/running work cannot release. No claim expires automatically.

After a lost acquisition/release response, fresh `guard.py recover HTML --mission M1 --expected CURRENT_SHA --request-id KEY` attaches a matching existing remote claim. It never reacquires or returns a call permit. A remote release also requires matching conclusive local evidence. If no matching claim is observed, retain uncertainty, inspect the actual service and reconcile. A restored or compacted coordinator follows this route before considering any new assignment.

All coordinators use **one common claim remote**, regardless of host/workstream. A canonical HTTP(S) ticket URL selects `refs/heads/claims/<SHA256>`; preserve its case-sensitive path and use the same canonical locator across callers. Reject aliases with credentials/query/fragment. The Git claim is an exclusion token, while the HTML holds operational state. Exact old-value leases and fresh reads admit one competing acquisition. Held claims block even identical requests. Releases append conclusive provenance; new work uses a distinct request ID. Never delete/rewind claim refs or steal an old claim.

This cooperative exclusion assumes preserved authority history. If administrators/other clients can erase a claim before a fresh coordinator observes it, the helper cannot reconstruct that lost history. Authority-side no-deletion/non-fast-forward protection or a durable independent anchor is required for that stronger guarantee; it has not been provisioned by installing this skill. Local bare paths are supported for tests, but are not portable host-loss authorities. Transport uncertainty always blocks automatic resend.

**Done when:** an actual receipt is recorded, active/uncertain work retains its claim, and only conclusively finished/non-delivered work can release it. Service lookup/idempotency remains necessary for uncertain external execution.

## Verify proofs against current sources

At independent verification, bind each criterion's actual evidence to the observed source set in `guard.proof_sources: {"SOURCE_ID":"FINGERPRINT"}`. Record this only for evidence actually verified against those versions. Before acceptance run `guard.py check HTML --mission M1 --expected CURRENT_SHA --purpose accept`, after conclusively releasing the finished run's claim. Every criterion needs verified proof or a not-applicable reason; all expected sources must still be unchanged and exactly match the proof binding.

A changed source invalidates eligibility for acceptance. Reconcile the new requirements/revision and reverify affected proofs. Capturing newer expected fingerprints does **not** update old proof bindings or establish that an earlier run used those versions. A green freshness check still leaves semantic criteria, delivery, independent reception and human gates to their actual evidence.

**Done when:** proof bindings and fresh observations agree for every required source, and every current criterion and mandated delivery gate has been verified. Save the result and continuation in the same HTML.
