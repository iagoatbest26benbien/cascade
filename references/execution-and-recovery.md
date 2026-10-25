# Execution and recovery

Use this reference when assignments are delegated, events repeat, or coordination resumes without the previous conversation. Keep manual handoffs lightweight: a saved prompt and its next action are enough when dispatch is outside the mandate.

## Define the pass

A **task** is a repeatable kind of work, such as implementation or review. A **run** is one execution, with its own identity and outcome. A **trigger** starts a run; it does not prove that the work is still needed. A mission may span several runs without changing ticket identity.

Record a compact execution contract in the existing tracker's `decisions`, linking supporting evidence:

- What starts this task, and what current prerequisite makes it eligible?
- What may it read and change, on which repository, branch and work surface?
- Which observed result makes subsequent work eligible?
- Which decision returns to a human, and who owns it?
- How are duplicate events and concurrent ownership handled?

Use the existing services: the ticket tracker holds requirements and dependencies; the git host holds code, PRs and checks; the orchestrator holds task identities and liveness. Link logs as evidence for the coordinator to reconcile. The HTML remains a dated operational view, not a scheduler or a lock. Identify the actual service for each role only where the execution needs it; designing or provisioning a new factory is a separate task.

## Dispatch once, then reconcile

Before dispatch, reread the ticket, relevant head/PR and orchestrator state. Match the mission, task kind and work revision to any existing run. A repeated event ID is the same delivery; different events can still refer to the same active work.

Use the platform's claim, concurrency or idempotency mechanism when available. Apply this write-ahead boundary to **every authorized outbound assignment**, including a follow-up in an existing chat:

For the supplied tracker, use [dispatch guards and freshness](dispatch-guards.md) to execute these checks and save/reserve the exact intent with `guard.py prepare`. Its shared Git claim excludes concurrent coordinators; retain the service's own idempotency and lookup. Record observed receipts with `guard.py record`, reconcile lost claim responses with `recover`, and release only from conclusive evidence. Other tracker formats retain their reader and require equivalent checks/atomic claims, or one serialized sender. Manual prompt handoffs remain lightweight.

1. Archive the exact tool payload bytes and digest. Revision-check/save the mission's `dispatch` with that payload reference/digest, destination, work revision, requested provider/model/options and available client request identity. Set delivery `unknown` and `continuation.current_action.phase` to `dispatch_pending` before the tool call. Reserve any correction attempt the call may consume against the same finding's cumulative budget. If the platform has no client key, save available correlation facts without inventing one.
2. Only after the local save succeeds, make the outbound call. Publish this intent off-host first when host-loss recovery is required.
3. Save the actual receipt, returned provider/model/options and observed delivery/execution state. Use `await_result` for confirmed running work and `verify` for a received result. Keep acceptance separate from both.

Keep the request identity and exact original payload stable across an authorized transport retry. A new correction round has a new run identity linked to the same finding and attempt history. The [continuation contract](long-context.md) records which action and decisions were incorporated.

If simultaneous coordinators cannot claim work atomically, serialize dispatch under one coordinator. Merely writing an owner into HTML does not exclude another sender. An uncertain send stays `unknown` until lookup confirms acceptance or non-delivery; recover by mission, request or run ID before resending. This includes an interruption after the pre-call save but before the call, and acceptance with a lost receipt. Resolve that pending intent before reusing its execution slot or attempt budget. Confirmed acceptance attaches the original receipt; confirmed non-delivery permits retry within the existing policy. Inconclusive lookup permits no automatic resend. Confirmed active work keeps its owner. Without service lookup/idempotency, a local skill cannot guarantee exactly-once execution.

Select the requested provider/model from live capabilities and pass supported model options through the dispatch tool. Retain the returned selection and task ID. Follow that environment's rules for continuing an existing task versus starting a new review round; a backing conversation ID is not automatically a continuation API.

When intake is in scope, search the ticket tracker before creating a ticket. Match the source's stable problem identity to an existing ticket, then append or reopen within the mandate. Deduplicating events alone does not prevent duplicate tickets.

## End a pass with a resumable outcome

Reconcile each run's evidence against the ticket, current code revision and required target. Worker completion makes a mission ready for verification; acceptance follows verified criteria and delivery obligations. Apply the project's checks, independent review and human gates where required.

Bind verified proofs to the actual ticket/code/CI source fingerprints and run the [freshness acceptance gate](dispatch-guards.md#verify-proofs-against-current-sources). Source changes or failed observations block acceptance; recapturing expectations never refreshes an older proof.

Automatic rework needs a finite attempt limit and an escalation owner. Use the project's or user's limit; if none exists, prepare one targeted follow-up and record that further automatic retries require a decision. Count attempts for the same unresolved finding across restarts. Stop dispatching against an unchanged blocker or an exhausted budget; progress independent missions.

An unattended pass that needs a human decision saves the blocker, owner and exact condition for resuming, then ends. In an interactive session, use the available question mechanism while continuing independent work. For running children or CI, use the platform's completion notification or watcher where available; record what will resume coordination. Distinguish a proposed trigger from an actually registered one. Creating schedules or subscriptions requires the user's mandate.

For disposable runners or transfer to another host, follow [durable checkpoints](durable-checkpoints.md). It supplies publication, integrity verification, dependency inclusion and restoration commands. Preserve implementation code through its independently retrievable repository and exact pushed revision; a coordination checkpoint does not automatically back up an implementation worktree.

In `journal`, record the trigger/event, mission, run/request ID, owner, work revision, observed status, attempt count and evidence location as dated text. In `next_action`, name the next eligible work or the exact event/decision to await. Use these existing fields rather than introducing a second maintained register. The next coordinator must be able to recover the state without relying on the last chat.

For long runs, promote current request identities, uncertainty and finding budgets into mission metadata, and keep the current mandate in the HTML's `continuation` object. Follow [bounded context recovery](long-context.md); a journal tail alone may omit the decisive older constraint or accepted request.

## Source and adaptation

Adapted from [software-factory, PR #1316](https://github.com/mattpocock/skills/pull/1316), reviewed at [b59cab42](https://github.com/mattpocock/skills/blob/b59cab42bf1206e1363eafc37f71b21c3bcaa12f/skills/in-progress/software-factory/SKILL.md). Its task/run distinction and design questions help authorized execution. Its remote-only architecture is not imposed on local coordination; the existing tracker, branch strategy and project delivery gates remain applicable.
