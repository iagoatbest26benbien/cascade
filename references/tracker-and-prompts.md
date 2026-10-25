# Tracker and prompts

## Active file

The `assets/tracker.html` template is self-contained and works offline. Its `id="tracker-data"` JSON block holds the state; the view and counters are derived from it. The browser displays state and copies prompts; it does not save changes. Updates go through the local file.

Replace `<skill>` with the absolute installed skill path:

```bash
python <skill>/scripts/tracker.py init /absolute/workstream/tracker.html --title "Workstream title" --spec "Specification URL or path"
python <skill>/scripts/tracker.py read /absolute/workstream/tracker.html > /temporary/state.json
# Edit the temporary JSON after rereading the active tracker.
python <skill>/scripts/tracker.py save /absolute/workstream/tracker.html --data /temporary/state.json
python <skill>/scripts/tracker.py check /absolute/workstream/tracker.html
```

`read` adds `_base_sha256` to the temporary editing JSON. Keep it when editing: `save` compares that revision under a local writer lock before changing the HTML or exporting prompts. A conflict requires a fresh read and reconciliation, preserving the other writer's facts. Authored JSON can instead supply `--expected` with the SHA-256 of the HTML it was based on. This editing metadata is never stored in the tracker.

`init` refuses to overwrite a file. `save` validates dependencies and exports populated prompts under `prompts/`. New exports become visible only after complete staging; an interrupted save can be resumed with the same intent after rereading. An existing prompt with different content requires a new follow-up filename. Prompt filenames are relative to the HTML directory; all paths **inside prompt text** are absolute. `read` and `check` are read-only. All local writers must use `save`; its POSIX sidecar lock coordinates that file only, not dispatch across machines.

## Data

Root fields: `title`, `spec`, `scope`, `updated_at`, `next_action`, `decisions` (text entries including owner and source), `missions`, and `journal` (dated text entries). Include timezone offsets in timestamps. Preserve older journal entries.

For long runs or compaction, keep the optional `continuation` mandate and current execution metadata described in [bounded context recovery](long-context.md). Use its read-only overview and paginated reads; full state and prompts remain in this HTML.

Mission shape; replace the explanatory text with current facts:

```json
{
  "id": "T-001",
  "title": "Actual ticket title",
  "ticket": "Ticket URL",
  "status": "to-prepare",
  "depends_on": [],
  "owner": "Known owner or unassigned",
  "chat": "Verified conversation link supported by the environment, or empty string",
  "workspace": "Known repository, branch, and path or unassigned",
  "checked_at": "Verification time with timezone",
  "criteria": [
    {"text": "Current ticket criterion", "result": "unverified", "proof": ""}
  ],
  "evidence": ["Verified stage, result, SHA/environment, source, and date"],
  "remaining": ["Specific unfinished work"],
  "blocker": "Exact prerequisite, owner, and release condition, or empty string",
  "next_action": "Concrete next action",
  "report": "Absolute path to latest report or empty string",
  "prompt": {
    "kind": "implementation",
    "file": "prompts/T-001-implementation.md",
    "delivery": "prepared",
    "text": "Full copyable prompt text"
  }
}
```

Omit `prompt` until a prompt is ready. Keep `evidence` concise, with pointers rather than copied logs. Keep secrets and personal data out of the tracker and prompts.

Mission states: `to-prepare`, `ready`, `running`, `to-verify`, `follow-up`, `blocked`, `done`. `ready` requires satisfied prerequisites; `running` requires confirmed execution. `depends_on` lists mission IDs whose completion gates this work. Record external prerequisites in `blocker`.

Criterion results: `unverified`, `passed`, `failed`, `not-applicable`. `proof` records the supporting reference or non-applicability justification. `done` requires every criterion passed or justified as not applicable, no remaining work or blocker, and delivery obligations verified by the coordinator. The script validates structure; it does not read evidence or decide acceptance.

Prompt kinds: `implementation`, `follow-up`. Transmission: `prepared`, `sent`, `unknown`. A sent prompt proves neither execution nor completion. For each follow-up, preserve the previous file, record its path in `journal`, and replace the current prompt reference.

For authorized dispatch, store the returned task/run ID and actual provider/model selection in dated `journal` entries, alongside the request/event identity, work revision, attempt history and evidence location. Use `owner` and `workspace` for responsibility and work surface; use `chat` only for a verified conversation link. Record the execution contract and retry limit in `decisions`, and the next trigger or human decision in `next_action`. These are records for reconciliation, not an atomic dispatch lock. See [execution and recovery](execution-and-recovery.md) when this mode applies.

When recovery after host loss is required, use [durable checkpoints](durable-checkpoints.md). Keep a mission's stable request identity and exact payload digest in its optional `dispatch` object; the checkpoint preserves original payload bytes through relocation and republication. A missing local reference in `proof`, `evidence` or `report` blocks a recoverable checkpoint; planned output destinations belong in the prompt or next action.

## The two prompts

### Implementation — the usual case

```text
[$implement](/absolute/path/to/implement/SKILL.md) https://github.com/organization/repository/issues/123

First read /absolute/workstream/tracker.html, mission T-001, then the ticket and repository instructions. Implement every criterion and complete the delivery required by those instructions. [Add only a necessary scope clarification.]

Save the report and evidence under /absolute/workstream/evidence/T-001/. Report passed and remaining criteria, commits, validations with evidence, and any blockers. Reread the tracker before concluding and report any discrepancy; the coordinator maintains it.
```

Replace variables and remove explanatory brackets. When the ticket and repository instructions suffice, keep the prompt this short. Do not copy all repository rules or systematically add audits, acceptance campaigns, or deployments outside the ticket.

If the project uses a different implementation skill or command, replace the first line with that explicit invocation. If it uses no implementation skill, state the implementation task and ticket URL directly, retaining the tracker pointer, scope, evidence destination and project delivery requirements. `$implement` is a convention, not a bundled dependency.

### Follow-up — incomplete ticket

```text
[$implement](/absolute/path/to/implement/SKILL.md) https://github.com/organization/repository/issues/123

Resume this mission in the existing chat and working files. Read /absolute/workstream/tracker.html, mission T-001, and /absolute/workstream/evidence/T-001/REPORT.md.

Verified results to preserve: [result and evidence pointer].
Observed remainder: [missing criterion and concrete observation].
Required work: [remaining correction or delivery].
Completion evidence: [observable outcome and evidence for each gap].

Finish the ticket's remaining criteria and delivery obligations. Reuse applicable evidence and justify what needs rerunning. Save this follow-up under /absolute/workstream/evidence/T-001/follow-up-01/ and return the delta, evidence, and any blockers. Reread the tracker before concluding; the coordinator maintains it.
```

A follow-up can stay short. Its value is the verified delta, not a complete new framing. An unchanged external blocker remains recorded until it is resolved.
