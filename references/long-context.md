# Bounded context recovery

For guarded dispatch, expand each relevant mission's `guard` and `dispatch` pointers: source expectations, proof bindings, claim authority/state and exact tool-envelope digest survive in the HTML. `unreleased_claim` flags include held/acquiring tokens even on a mission otherwise marked done. After compaction, [recover and reconcile the existing claim](dispatch-guards.md#record-observations-and-recover-uncertainty); no compact view, restored token or previous preparation result grants another call permit. Reobserve required sources before dispatch or acceptance.

Use this for long workstreams, context compaction, or a coordinator returning without the previous conversation. The HTML remains the only maintained operational register. Its full journal, prompts and evidence stay intact; load a bounded view, then retrieve the relevant details.

## Recover the entry point

At activation of delegated or multi-turn coordination, register the existing tracker in the workspace's `AGENTS.md`. The helper preserves unrelated instructions and adds only stable workstream locators, not operational state:

```bash
python -B <tools>/bootstrap.py locate /absolute/workspace
# For existing AGENTS.md, pass its returned agents_sha256 to bind.
python -B <tools>/bootstrap.py bind /absolute/workspace \
  --tracker /absolute/workspace/.scratch/workstream/tracker.html \
  --id project/workstream --expected AGENTS_SHA256
```

After interruption, compaction or a new coordinator, reread workspace instructions and the installed skill. Run `bootstrap.py locate WORKSPACE`; select the matching canonical source, using `--id` when necessary. A sole registered entry identifies a location, not permission. Several entries stay ambiguous until the surviving mandate/source identifies one. Missing, stale or mismatched locators require source recovery before assignment; never choose by modification time or silently create a replacement tracker. Existing instructions supplying an unambiguous locator may be used to register the same entry.

The locator survives loss of chat within a surviving workspace. After restoration at another root, bind the verified restored tracker into that workspace's instructions before resuming. Losing the workspace too requires the off-host bootstrap described in [durable checkpoints](durable-checkpoints.md).

An unbound legacy tracker can use a canonical URL spec or an existing local spec inside the workspace. Local sources are registered as `workspace:relative/path`; relative specs resolve from the tracker directory and retain their meaning when the workspace moves intact. A spec outside that root needs an explicit `workstream: {id, source}` canonical URL binding in the HTML, established from the mandate/source relationship before registration. The locator checks source location/identity; it does not certify spec contents or permissions.

### Existing trackers of another format

Keep their format and existing reader. `bootstrap.py`, `resume.py` and `checkpoint.py` parse the supplied template; their JSON checks do not apply to another format. Use this explicit **manual locator-only route** instead of migrating solely to satisfy a helper:

1. Read the existing tracker and authoritative mandate/specification. Confirm its stable workstream identity and source relationship. Add a plain pointer to workspace `AGENTS.md`, preserving other instructions, for example: “After interruption or compaction, resume workstream `project/export` from `.scratch/export/tracker.html`, source `docs/export-spec.md`; reread the installed Cascade skill and reconcile current action, decisions and pending sends before acting.” Use actual verified paths/identity, with no operational state or permission in this pointer. Keep manual pointers outside the helper's managed block.
2. On recovery, read these workspace instructions and use the tracker format's reader. Match its identity/source to the surviving mandate. Multiple matches remain ambiguous; stale/missing paths or source mismatches require source recovery before assignment. Helper discovery returning no registration does not invalidate this explicit pointer or authorize a duplicate tracker.
3. In the **same existing operational file**, retain the equivalent mandate, sources, holds, current action/phase, pending request receipts/payload references, cumulative budgets, and which recorded instructions/decisions were incorporated. Compare later changes and relevant authoritative sources before action; reconstruct missing fields first. Use that format's existing revision checks where available, or verify the file SHA before saving and serialize writers. A locator-only success cannot certify eligibility, freshness or a complete continuation.

The following JSON fields, fingerprint helper and CLI gate describe the supplied template. Their behavioral requirements above still apply to other formats. For a custom tracker, use its established verified archival/restoration procedure if host loss is in scope; the bundled checkpoint parser cannot validate arbitrary formats.

## Leave recoverable current state

For delegated or multi-turn coordination using the supplied template, keep this `continuation` object inside the same HTML, updated through the ordinary revision-checked save. Legacy template files remain readable; reconstruct an incomplete contract before resumed delegated or automatic action:

```json
{
  "mandate": "Current user goal, authorized actions, workstream scope and required provider/model options",
  "sources": ["Exact source of the user mandate and subsequent scoped changes"],
  "constraints": ["Current holds, affected missions, owner and release condition; other active limits"],
  "read_next": ["tracker:/missions/42/dispatch", "Relevant evidence, ticket or native run/task locator"],
  "current_action": {
    "phase": "reconcile",
    "mission_ids": ["T5"],
    "next_step": "Look up the saved request before any retry",
    "sources": ["tracker:/missions/42/dispatch"]
  },
  "reconciled": {
    "journal_sha256": "ACTUAL_JOURNAL_FINGERPRINT",
    "decisions_sha256": "ACTUAL_DECISIONS_FINGERPRINT",
    "state_sha256": "ACTUAL_CURRENT_STATE_FINGERPRINT"
  }
}
```

Record the actual mandate, with source pointers and concise facts. Refresh it when the user changes the goal, permissions, model choice or a hold, and before a handoff. Save these changes and the current action together before dependent actions, and after each observation. An environment may compact without warning; a pre-compaction hook is supplementary. Existing authorization carries forward within its scope. A recovered record does not extend that scope, and the latest applicable user instruction takes precedence. A generic “continue” does not establish that a specific hold was released.

Action phases are `reconcile`, `prepare`, `dispatch_pending`, `await_result`, `verify`, `await_decision`, and `idle`. Use actual mission IDs, or an empty list for a workstream-wide action. A received worker result checkpoints `verify`, not acceptance. An uncertain outbound call checkpoints `dispatch_pending` and resumes with lookup. A hold checkpoints the affected action at `await_decision`; independent work can still proceed within the mandate.

`reconciled` binds this checkpoint to its journal, decisions and current coordination state. Set all action, mandate, constraint and mission fields first; then compute `resume.continuation_fingerprints(state)` on the temporary editing JSON and assign that result to `continuation.reconciled`. The overview's `reconciliation_fingerprints` describes the currently saved state, so copying it is appropriate only when no covered state changes remain to be made. Save the reconciled action and fingerprints together. Added/edited history or mission-only changes (holds, budgets, dispatch, reports, work revision), source/mandate changes and edited actions make an old checkpoint `stale`, even when the HTML SHA is fresh. Review the changes before refreshing fingerprints. Save timestamps, temporary `_base_sha256` and the fingerprints themselves are excluded from the state digest. A prior history-only checkpoint remains readable but is incomplete until freshly reconciled with the state fingerprint. These hashes establish consistency with recorded state; they cannot prove that the mandate is current or that a caller actually read it. Refresh relevant external sources before acting.

Use `tracker:/...` for stored JSON-pointer locators so they are distinct from filesystem dependencies. Pass the part after `tracker:` to `--pointer`. Local evidence uses actual paths; services use full URLs.

Keep current execution facts in each mission, alongside their history:

- `dispatch`: known request/run/task IDs, delivery outcome, work revision, exact payload digest and actual provider/model/options. Omit unknown identities rather than inventing them.
- `attempts`: finding identity with cumulative `used`, `limit` and escalation owner, for example `{"F-9":{"used":2,"limit":2,"next_owner":"reviewer"}}`. An absent history or limit is unknown, not zero.
- `hold`, when needed: `active`, affected scope, source, decision owner and `release_condition`. Keep the blocker and next action consistent with it.
- `checked_at`, report/evidence pointers and tested revision: enough to distinguish an earlier success from evidence for current work.

For **every authorized send**, persist the [write-ahead intent](execution-and-recovery.md#dispatch-once-then-reconcile) before the outbound call, including `unknown` delivery. This applies to ordinary compaction as well as host loss. Reserve any correction attempt that the pending call may consume. Save the receipt afterward; an interruption at either boundary leaves reconciliation work rather than another automatic send.

The journal preserves events; current safety-critical facts must also be discoverable in these mission fields. Resolve a known request before retrying, and carry its finding budget across restarts. Record registered watchers or completion triggers by their actual identifiers; a suggested watcher is still only a suggestion.

## Recover in small reads

Replace `<tools>` with the installed skill's `scripts/` directory, or the fetched checkpoint's `tools/` directory:

```bash
python -B <tools>/resume.py /absolute/workstream/tracker.html

# After reconstructing/reconciling the complete continuation, check its structure
# and history/state fingerprints before resumed delegated or automatic action.
python -B <tools>/resume.py /absolute/workstream/tracker.html \
  --expected HTML_SHA256 --require-continuation

# Use revision_sha256 and next_offset from the response.
python -B <tools>/resume.py /absolute/workstream/tracker.html \
  --expected HTML_SHA256 --offset NEXT_OFFSET

python -B <tools>/resume.py /absolute/workstream/tracker.html \
  --mission T5 --expected HTML_SHA256

python -B <tools>/resume.py /absolute/workstream/tracker.html \
  --pointer /missions/42/dispatch --expected HTML_SHA256

# Search exact journal strings without loading the entire history.
python -B <tools>/resume.py /absolute/workstream/tracker.html \
  --pointer /journal --contains request-017 --expected HTML_SHA256

# Retrieve an exact long string in successive character chunks.
python -B <tools>/resume.py /absolute/workstream/tracker.html \
  --pointer /missions/42/prompt/text --offset 0 --limit 2000 \
  --expected HTML_SHA256
```

The default overview is limited to 12,000 UTF-8 output bytes. `--max-bytes` changes that bound. It counts flags across **all** missions, including completed ones, and prioritizes recorded uncertainty, holds and exhausted budgets. Counts classify known structured fields; free-text constraints and missing metadata remain unclassified. Recorded flags are leads for reconciliation, not proof of live execution or eligibility.

Follow `next_offset` until the relevant inventory is covered, and expand any needed `expand_pointers`. A partial or budget-exceeded view has not supplied the missing facts. Read the complete current mandate, action and applicable constraints before acting. `continuation_recovery_work` lists missing fields or changed history/state. `--require-continuation` rejects `missing`, `incomplete` and `stale` contracts without stdout; `recorded` describes consistency with saved state only, including waiting states. It does not declare work eligible. Recover missing facts from current user instructions and recorded sources; inaccessible mandate/hold evidence blocks the affected action. Historical reads remain available. Do not infer permission from a recent summary or another agent's report.

Every selected detail and subsequent overview page requires the exact HTML SHA from the overview. A changed SHA requires a fresh overview and new pointers. That guard prevents mixing tracker revisions; it does not refresh tickets, CI, worker liveness or target evidence. Compare the actual report's revision, scope and target with current work before accepting it. An older “all ready” note cannot override current blockers or an uncertain send.

String chunks preserve the exact embedded text. For dispatch, use the saved prompt bytes and, for uncertain historical sends, the original payload archive described in [durable checkpoints](durable-checkpoints.md). A view or relocated prompt is not a substitute request payload.

## Save without flooding the model context

`resume.py` is read-only. Its JSON is a derived view, never editing input or a second register. For changes, use `tracker.py read ... > /temporary/state.json`, edit only the relevant fields programmatically without printing the whole JSON, then `tracker.py save ... --data /temporary/state.json`. Preserve `_base_sha256`, unrelated missions and history; reconcile a stale save.

Fetch only the applicable repository instructions, current ticket criteria and relevant evidence. Keep large logs in linked evidence files. Give workers the mandate and scope relevant to their assignment, the tracker/mission pointer and source locations, rather than a copy of the previous chat. For a large tracker, point them to this bounded-read workflow.

After source-host loss, restore the checkpoint first. New checkpoints bundle `resume.py`; old checkpoints remain readable with the installed or shared repository's current tools. Keep a bootstrap locator outside the chat and source disk. This workflow makes tested recovery independent of chat history; it does not increase a model's context window or guarantee recovery of instructions that were never recorded.
