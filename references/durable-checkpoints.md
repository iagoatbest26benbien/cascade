# Durable checkpoints

Read this before dispatch on a disposable host, before handing coordination to another host, or when recovering after source-disk loss. The canonical working register remains the HTML; a checkpoint is a versioned transport of that register and its required files.

## Choose a recoverable destination

Use the [configured shared repository](shared-repository.md) and its registered workstream branch, or the user's explicitly selected alternative. The shared repository serves all authorized orchestrators; its default `main` branch holds the directory, while each workstream has a stable recovery branch. Keep the repository/branch locator discoverable outside the source host, through the shared directory, an authorized ticket field, or handoff to the user. A receipt left only beside the lost HTML cannot bootstrap recovery.

Provision credentials through the environment's normal credential helper. Store no tokens or authenticated URLs in tracker data, prompts, receipts or checkpoints. Private Git storage controls access; checkpoint hashes verify bytes, not confidentiality or permanent retention. Apply the repository's retention and access policy.

The checkpoint includes the HTML, current and historical prompts, reports, evidence, necessary context and explicitly included dependencies. Required local inputs outside the workstream must be included or moved to a stable, independently verifiable external source. Preserve original evidence and sent payloads exactly; relocated active files are derived working views.

New snapshots also bundle `guard.py`, `claims.py` and `sources.py`. Guarded tool envelopes under `evidence/dispatch/` remain exact evidence bytes, with identity/digest under `dispatch.tool_payload_file` and `tool_payload_sha256`; the older `payload_sha256` field still names exported prompt bytes. Preserve the configured off-host claim authority and pending claim/request across restoration. Recover the existing token without issuing a new send permit, following [dispatch guards](dispatch-guards.md). Never copy a local test authority into a new independent lock service.

The default selection is the tracker, `prompts/`, `evidence/`, `dependencies/` and `context.md`. Other required files need an explicit `--include`; a checkpoint is not an indiscriminate copy of the directory or home folder. Keep required reports under `evidence/` and name their paths in the tracker. To retain a report's `../artifacts/command.log` link, include an existing source-internal directory at the same location: `--include /absolute/workstream/artifacts=artifacts`. External inputs stay under `dependencies/`.

Implementation repositories remain separate: record their retrievable remote and exact revision. Unpushed code or a missing external dependency is an unresolved recovery prerequisite. Publishing the coordination files alone does not prove preservation of that code.

For `guard.sources` of kind `git`, the [explicit source recovery locator](dispatch-guards.md#establish-the-contract-in-the-html) binds a separate remote/full commit to its expected fingerprint. Creation verifies retrieval and records the inventory; offline verification preserves that dated evidence. Restore keeps the checkout locator without automatically cloning/executing the repository. Rehydrate it at the pinned commit before clearing the unknown-source gate. JSON prompt/request pointers are typed metadata, preserved as such. Declared current and historical tool envelopes must be present and match their exact recorded digests before snapshot, verification or restoration succeeds.

## Publication and restoration

The bundled `scripts/checkpoint.py` provides publication, verification and restoration with Python's standard library and Git. Substitute the authorized endpoint, branch and absolute paths:

```bash
python <skill>/scripts/checkpoint.py publish \
  --root /absolute/workstream \
  --remote https://github.com/ORG/PRIVATE-COORDINATION.git \
  --branch recovery/WORKSTREAM --expected absent \
  --workstream-id PROJECT/STABLE-WORKSTREAM \
  --include /absolute/implement=dependencies/implement

# For later publications, --expected is the previous receipt's full commit SHA.
python <skill>/scripts/checkpoint.py verify \
  --remote https://github.com/ORG/PRIVATE-COORDINATION.git \
  --branch recovery/WORKSTREAM --expected CHECKPOINT_SHA \
  --expected-workstream-id PROJECT/STABLE-WORKSTREAM
```

For the shared repository, select its registered workstream ID and exact branch; use the same ID after restoration. A standalone destination without a shared catalog can keep an unbound checkpoint by omitting the identity options. Binding a legacy local specification requires an explicit source check, not an inferred match from its directory name.

Include the containing skill directory when it has relative references; include each additional required specification or input explicitly. The helper rejects uncaptured local references, divergent prompt exports and missing local proofs. Express service endpoints as full URLs so they are distinct from filesystem paths. It checks explicit prose paths and Markdown links, including attachments linked from Markdown/text reports. Runtime dependencies and structured or opaque evidence still require the coordinator's inventory; historical machine paths in a JSON receipt are provenance, not automatically required attachments.

For local evidence, use an absolute path or a Markdown file link; plain relative paths under `evidence/`, `prompts/` and `dependencies/` are also checked. A prose statement that a test passed is not an archived attachment or command log.

The receipt contains the clean remote, branch, pinned commit and `remote_confirmed`. `disaster_recovery_tested` remains false until an independently recorded drill establishes it. Save the locator in the authorized handoff outside the source disk.

On a replacement host, bootstrap from the selected remote using separately provisioned Git credentials. The snapshot includes the recovery code; the installed skill and old home directory are unnecessary:

```bash
git clone --single-branch --branch recovery/WORKSTREAM \
  https://github.com/ORG/PRIVATE-COORDINATION.git /absolute/checkpoint-copy
git -C /absolute/checkpoint-copy checkout --detach CHECKPOINT_SHA
python -B /absolute/checkpoint-copy/tools/checkpoint.py verify \
  --snapshot /absolute/checkpoint-copy \
  --expected-workstream-id PROJECT/STABLE-WORKSTREAM
python -B /absolute/checkpoint-copy/tools/checkpoint.py restore \
  --snapshot /absolute/checkpoint-copy --destination /absolute/new-workstream \
  --expected-workstream-id PROJECT/STABLE-WORKSTREAM
python -B /absolute/checkpoint-copy/tools/tracker.py check \
  /absolute/new-workstream/tracker.html
```

Use a new destination and confirm the checked-out commit against the handoff before running its trusted helpers. The source snapshot is preserved under `.checkpoint/source/`; sent prompt bytes are additionally indexed by SHA-256 under `.checkpoint/originals/` and carried through republication. `manifest.json` binds those originals to their mission and recorded request context. Active paths are relocated for the new host. No evidence file is rewritten. Recovered working files can be published again using the current checkpoint as the expected parent.

For later same-workspace compaction, register the restored tracker in the replacement workspace's `AGENTS.md` with `bootstrap.py bind`, following [bounded context recovery](long-context.md). For a bound checkpoint, restoration copies its verified `workstream` identity into the derived active HTML, rejecting a conflicting identity; the archived source stays exact. Bind using that same ID. New snapshots bundle `bootstrap.py` alongside `resume.py`; older snapshots can use the current trusted installed/shared tools. A locator from the lost workspace is historical and does not point to the replacement root automatically.

Historical report links keep their original bytes. Capture relative attachments at the same relative locations; an absent target blocks publication. Executable regular files retain an explicit executable flag, restored as `0755`; other files use `0644`. Older checkpoints without mode metadata have unknown original executability. Omit the expected-ID option only for a deliberately selected unbound legacy snapshot, retaining its identity limitation.

When republishing after relocation, repeat custom same-relative includes with the new root. The archived manifest's `includes` is their inventory; required inputs stay included until their retirement is explicitly justified, including dependencies inside opaque artifacts.

Publish against the exact previous checkpoint revision, or an explicit absent head for the first publication. The helper creates a direct child of that parent and uses an exact Git lease for the branch update, including deletion/rewind races. A stale expected revision requires rereading the canonical checkpoint and reconciling; publication does not merge competing operational states. Keep one active dispatch coordinator or use the platform's claim mechanism. A rejected checkpoint update is not a dispatch lock.

Verify through a fresh remote read. A successful local commit or push response alone does not prove recoverability. Keep the returned pinned commit as the checkpoint identity and the branch as its discoverable current pointer. After a transport error, inspect that pointer and verify the intended revision before retrying publication.

To recover, fetch the authenticated checkpoint into a fresh location, verify its manifest and every managed file, then restore into a new workstream directory. Required absolute paths are relocated through declared mappings; historical evidence remains byte-identical in the source snapshot. Restore must reject missing, changed or unsafe members, and rerun the tracker and prompt-export checks before exposing a successful result.

## Checkpoint before the side effect

1. Update the HTML with the intended mission, exact saved payload, request identity, owner, work revision and attempt count. Set delivery to `unknown` for the pending send. Publish and verify this intent before sending it.
2. Send through the authorized orchestrator, using its idempotency or claim mechanism where available.
3. Save the returned task/message identity and observed status, then publish the updated checkpoint.

If the host disappears between steps 1 and 3, the restored record remains uncertain. Look up the same request in the orchestrator; use the same request identity for a transport retry only when non-delivery or provider deduplication is established. If the platform cannot resolve the uncertainty, block that dispatch and continue independent missions. Checkpoints do not guarantee exactly-once external execution.

A relocated copy of a sent or uncertain prompt is a reading aid, not the original request payload. Reconcile using the preserved original bytes and request identity. If a new work location requires a changed payload, resolve the earlier request first, then prepare a separately identified follow-up; reusing an idempotency key with changed bytes is unsafe.

Record request identity and the SHA-256 of the exact exported prompt bytes in the mission's optional `dispatch` object, for example `{"request_id":"request-017","payload_sha256":"<64 hex characters>","status":"unknown","attempt":1}`. Send those same bytes. The identity and digest remain unchanged by path relocation; retrieve the original from `.checkpoint/originals/<payload_sha256>.prompt` when reconciling an uncertain send. Use `journal` for the dated event, response and reasoning.

For older snapshots without a recorded digest, the helper carries the earliest archived payload associated with the same mission and request identity. Later relocated versions are labeled as views, without inheriting that dispatch identity. Reconcile this provenance before adopting an old uncertain request.

## Recovery acceptance

The handoff is recoverable when the next coordinator can use only the remote locator, separately provisioned credentials and documented bootstrap procedure to:

- retrieve the pinned snapshot without the original filesystem;
- verify all required bytes, including historical prompts, reports and attachments;
- restore a usable tracker and current prompts under a different root;
- recover mission identities, dependency states, owners, attempts and uncertain requests;
- identify missing external prerequisites before dispatch;
- resume by reconciling existing runs, with no duplicate send.

Record the actual drill and its limits: removing a fixture source directory and restoring from a real remote demonstrates source-file independence, while a separate-machine drill is stronger. Retest after changes to the checkpoint format, inclusion rules or restoration behavior. A fresh checkpoint still needs verification; previous recovery evidence does not certify bytes that were never uploaded.
