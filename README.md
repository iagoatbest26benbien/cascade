<p align="center">
  <img src="docs/assets/cascade-cover.png" alt="Cascade — Clear work. Shared memory. Reliable handoffs." width="100%">
</p>

<p align="center">
  <a href="https://github.com/nautalsoft/cascade/actions/workflows/ci.yml"><img src="https://github.com/nautalsoft/cascade/actions/workflows/ci.yml/badge.svg" alt="Validation"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6759df" alt="MIT license"></a>
  <a href="https://skills.sh/nautalsoft/cascade"><img src="https://skills.sh/b/nautalsoft/cascade" alt="Install Cascade on skills.sh"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776ab" alt="Python 3.11 or newer">
  <img src="https://img.shields.io/badge/runtime_dependencies-zero-237b57" alt="Zero third-party Python dependencies">
</p>

<p align="center">
  <b>A portable agent skill for taking a ticket graph from assignment to verified delivery.</b><br>
  Copyable prompts. Focused follow-ups. One offline HTML tracker that survives the conversation.
</p>

<p align="center">
  <a href="#start-in-two-minutes">Get started</a> ·
  <a href="docs/guide.md">User guide</a> ·
  <a href="docs/README.fr.md">Français</a> ·
  <a href="examples/README.md">Try the demo</a> ·
  <a href="SKILL.md">Read the skill</a>
</p>

## Keep the next action clear

When several tickets, agents and repositories move at once, the hardest question is often: **what can start now, and what evidence are we still missing?**

`cascade` gives the coordinator a repeatable workflow and a durable place to answer it. Requirements stay in your issue tracker. Code stays in your repositories. The HTML connects the work, the prompts, the conversations and the evidence.

| Capability | What you get |
| --- | --- |
| Dependency-aware planning | A ready frontier based on verified prerequisites and available work surfaces. |
| Useful assignments | Short implementation prompts and follow-ups focused on the observed remainder. |
| Shared operational memory | A searchable, responsive tracker with criteria, owners, blockers and copyable prompts. |
| Honest delivery states | Preparation, transmission, execution, tests, CI, deployment and acceptance remain distinct. |
| Continuity across sessions | A stable workspace locator and fingerprinted continuation for interruption or compaction. |
| Optional guarded execution | Shared Git claims, source freshness checks and write-ahead intents before authorized dispatch. |
| Optional off-host recovery | Integrity-checked checkpoints, pinned identities and restoration at another root. |

<p align="center">
  <img src="docs/assets/tracker-preview.png" alt="Example tracker showing a release workstream, ready missions, blockers and acceptance criteria" width="100%">
</p>

## Start in two minutes

You need **Python 3.11+** on **Linux, macOS or WSL**. The helpers use only Python's standard library. The HTML viewer works offline in a modern browser, including on Windows. Git is required for checkpoints and claims; authenticated GitHub CLI is required only for live GitHub source adapters.

Install with the [skills CLI](https://skills.sh/docs/cli) and choose your agent (Node.js 22.20+ is required for this installer):

```bash
npx skills add nautalsoft/cascade --skill cascade
```

Add `-g` to install across projects. Or use the dependency-free Python installer:

```bash
git clone https://github.com/nautalsoft/cascade.git
cd cascade

# Install for Codex and other hosts that discover ~/.agents/skills.
python3 tools/install.py --dest "$HOME/.agents/skills/cascade"
```

The installer copies the runtime skill and refuses to overwrite an existing destination. To update, install into a fresh directory, compare your local changes, then replace your previous copy deliberately. See the [installation guide](docs/guide.md#installation) for other hosts and project-local installation.

In your agent, invoke the skill with a concrete scope:

```text
Use cascade to coordinate these tickets: <ticket URLs>.
Read the repository instructions and current dependencies.
Create one shared HTML tracker, then prepare prompts for the ready work.
Hand the prompts to me for dispatch.
```

In Codex, you can start that prompt with `$cascade`. In Claude Code, use `/cascade`. Other hosts can load `SKILL.md` through their skill mechanism. [Host setup and sources →](docs/guide.md#agent-hosts)

**Already have an implementation workflow?** The default prompt convention is `$implement <ticket URL>`. That skill is not bundled: use your existing implementation skill or the project's equivalent explicit instructions. The coordinator works with either.

## Try it without an agent

Create a fictional four-ticket release train, with one accepted prerequisite, one ready mission, one running mission and one blocked mission:

```bash
python3 examples/create_demo.py /tmp/orchestrate-demo
python3 scripts/tracker.py check /tmp/orchestrate-demo/tracker.html
```

Open `/tmp/orchestrate-demo/tracker.html` in your browser. Search, filter by state, expand an implementation prompt and copy it. Everything in this demo is labeled fictional; nothing is sent to a service.

Or create an empty tracker for your own work:

```bash
python3 scripts/tracker.py init /tmp/my-workstream/tracker.html \
  --title "My workstream" --spec "https://example.com/specification"
python3 scripts/tracker.py read /tmp/my-workstream/tracker.html
```

Edit through the revision-checked `read → save → check` workflow in the [guide](docs/guide.md#maintain-a-tracker). The browser is a viewer: changes are saved by the Python helper.

## Choose the depth you need

1. **Prepare and hand off.** Read the tickets, record the dependency graph, prepare prompts and reconcile reports. No remote coordination service is needed.
2. **Delegate with guards.** When dispatch is authorized, add a user-owned claim remote, a current source contract and write-ahead records. The helpers reserve and record; your agent's own tools perform the authorized send.
3. **Recover across hosts.** When host loss matters, publish to a separate operational repository, verify a fresh fetch and complete a restore drill before relying on recovery.

Start with the first mode. Follow the references when your work needs the next one. Installation configures no remote, account, scheduler or outbound messaging.

## Under the hood

```text
SKILL.md              The coordinator's workflow
agents/               Optional host UI metadata
assets/tracker.html   Self-contained viewer + embedded state
scripts/              Tracker, continuation, claims and checkpoint helpers
references/           Detailed contracts, read only when needed
examples/             Reproducible fictional release train
tools/                Installation and repository validation
```

The tracker is a dated operational view. Tickets and evidence remain authoritative. A successful structural check does not independently verify a ticket criterion, and a local Git test does not prove hosted disaster recovery. Cooperative claims require retained Git history; external services still need their own receipt lookup or idempotency. [Execution contract →](references/execution-and-recovery.md)

## Contribute

```bash
python3 -B tools/check.py
```

This runs the helper and packaging tests plus repository checks. GitHub Actions runs the same entry point on Linux and macOS. See [CONTRIBUTING.md](CONTRIBUTING.md) for the scope of changes and visual checks.

Created by [Nautal Soft](https://github.com/nautalsoft). MIT licensed. Built for people and agents who want the next handoff to be as clear as the first. Adaptation credits are in [NOTICE.md](NOTICE.md).
