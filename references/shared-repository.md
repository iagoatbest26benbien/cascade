# Shared recovery repository

Remote coordination is optional. The public skill ships with **no configured remote**. Before guarded dispatch or off-host recovery, select a repository controlled by the user or their team, and record its URL in the workspace instructions or tracker decisions. Use a private repository when prompts, tickets or evidence contain private information. Keep that operational repository separate from this public skill repository.

The examples use `https://github.com/ORG/PRIVATE-COORDINATION.git` as a placeholder. Replace it with the selected real remote; never use another person's recovery repository.

All authorized coordinators within that coordination scope reuse the same remote for checkpoints and claims. A new invocation, agent, model or restored machine does not require a new repository or a new workstream. Respect an explicitly selected project-specific destination when the user provides one. Start with an initialized default branch containing a `registry.json` object with `{"schema":"orchestrate-tickets/catalog/v1","workstreams":[]}`; see the helper's `catalog --help` and the registry format below. Creating or publishing an operational remote requires the user's authorization.

## Find the existing workstream first

The default `main` branch is the repository's entry point. Read its `README.md`, `AGENTS.md` and `registry.json`. The registry is a directory of workstreams, not a second ticket tracker. Current mission states, dependencies and evidence remain in each workstream's HTML.

```bash
python <skill>/scripts/checkpoint.py catalog \
  --remote https://github.com/ORG/PRIVATE-COORDINATION.git

python <skill>/scripts/checkpoint.py catalog \
  --remote https://github.com/ORG/PRIVATE-COORDINATION.git \
  --id PROJECT/STABLE-WORKSTREAM
```

Match the project and canonical ticket/specification source before selecting an ID. The command resolves each registered branch to its current remote commit and reports unregistered recovery branches. It does not infer live worker status, authorize a takeover or verify a checkpoint. Inspect and reconcile an unregistered matching branch before creating another one.

Keep the same ID and branch for a resumed workstream or coordinator handoff. Use a stable `project/workstream` identity; models, chat titles, dates and invocation numbers are not workstream identities. New snapshot branches follow `recovery/<project>/<workstream>`. Preserve any existing legacy branches and their recorded identities.

Authorized guarded dispatch also reuses this common remote for `claims/<canonical-ticket-hash>` branches, following [dispatch guards](dispatch-guards.md). These append-only exclusion tokens are separate from `main`, recovery snapshots and registry entries. Preserve them during publication; do not register them as workstreams, delete them, expire them or rewrite their history. They rely on cooperative writers unless authority-side protection or a durable anchor establishes stronger retention.

## Register a genuinely new workstream

Add one entry to `main:registry.json` before its first checkpoint:

```json
{
  "id": "project-a/export-v2",
  "project": "project-a",
  "title": "Export v2 delivery",
  "source": "https://github.com/ORG/PROJECT/issues/123",
  "branch": "recovery/project-a/export-v2",
  "owner": "Stable coordinator or conversation link",
  "status": "active"
}
```

IDs, branches and canonical sources are unique across the registry. Record owner handoffs and coarse directory status (`active`, `blocked`, `completed`, `archived`, or `example`) in the same entry. Keep completed entries discoverable; the registry does not replace ticket acceptance evidence.

Update `main` from its latest remote revision and publish the registry change conditionally against that exact revision. If another coordinator updated it, reread and reconcile the directory instead of overwriting it. Keep unrelated entries, docs and tools. Reread the remote registry and rerun `catalog` after registration. Registration gives a workstream a location; ownership and dispatch claims still follow the orchestrator's concurrency rules.

Publish checkpoint files only to the registered recovery branch, selecting `--workstream-id PROJECT/STABLE-WORKSTREAM`. The checkpoint carries that stable identity across restorations; changing the directory path or owner leaves it intact. The helper checks the registered branch and inherited identity before publication, and protects the branch containing the root registry. A lease guards the revision; the workstream binding guards which work it contains.

When verifying or restoring a bound checkpoint, pass `--expected-workstream-id PROJECT/STABLE-WORKSTREAM` to reject a locator for another workstream before exposing active files. Older unbound snapshots remain readable; establish their source and identity explicitly before adopting one for shared publication. Unbound legacy snapshots remain usable without pretending that identity was already recorded.

For an unbound local specification, record its verified relationship to the registered canonical source in the tracker's decisions, then publish with `--adopt-workstream-source CANONICAL_SOURCE_URL`. This explicitly acknowledges the source mapping; the helper cannot infer it from a local filename. A bound checkpoint retains its identity and cannot use this option to become another workstream.

A saved SHA in an old chat is a historical checkpoint; `catalog` gives the current branch head. Use the selected pinned commit for a specific restoration, then [verify and restore](durable-checkpoints.md) before any assignment.
