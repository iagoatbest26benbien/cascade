# User guide

[Overview](../README.md) · [Français](README.fr.md) · [Demo](../examples/README.md)

## Installation

With Node.js 22.20+ and the [skills CLI](https://github.com/vercel-labs/skills), install directly from GitHub:

```bash
npx skills add iagoatbest26benbien/cascade --skill cascade
```

Choose your agent interactively. Add `-g` for a personal installation across projects, or `--agent codex` to select Codex. The CLI's default installation telemetry powers discovery on [skills.sh](https://skills.sh/docs/faq); opt out with `DISABLE_TELEMETRY=1` if desired.

For a Python-only installation with explicit destination and overwrite refusal, use the bundled installer below.

Clone the repository, inspect `SKILL.md`, then choose a destination for the complete skill directory:

```bash
git clone https://github.com/iagoatbest26benbien/cascade.git
cd cascade
python3 tools/install.py --dest "$HOME/.agents/skills/cascade"
```

The destination must not exist. Your installed skill contains its instructions, runtime helpers, references, HTML template and host metadata. Keep the clone for examples, development tools and future updates. To remove the skill, remove only the destination you chose after preserving any local modifications. Store live workstreams in your project, outside the installed skill.

Python 3.11+ and a POSIX filesystem are required by the helpers (`fcntl` locks, atomic links and Git subprocesses). Use Linux, macOS or WSL for execution. Native Windows Python is not supported. No `pip install` is needed. A modern browser can open the HTML on any desktop platform.

## Agent hosts

These paths are documented discovery locations, not a claim that every host/provider combination has been runtime-tested. The workflow requires file access and a shell; delegated execution additionally requires the host's own task tools.

| Host | Personal destination | Invocation |
| --- | --- | --- |
| Codex | `~/.agents/skills/cascade` | `$cascade` |
| Claude Code | `~/.claude/skills/cascade` | `/cascade` |
| OpenCode | `~/.config/opencode/skills/cascade` | Ask it to load the `cascade` skill |
| Other Agent Skills hosts | The host's configured skill directory | Load the bundled `SKILL.md` and retain its relative resources |

For Claude Code, change only the installer destination:

```bash
python3 tools/install.py --dest "$HOME/.claude/skills/cascade"
```

For a project using Codex, run from this clone and supply an absolute destination such as `/path/to/project/.agents/skills/cascade`. Follow your host's project-local convention. Avoid installing the same skill in several discovery locations unless you want duplicate entries.

Discovery and invocation sources: [official OpenAI skill documentation](https://learn.chatgpt.com/docs/build-skills), [Claude Code skills](https://code.claude.com/docs/en/skills), [OpenCode agent skills](https://opencode.ai/docs/skills/). Checked on 2026-10-25. Host behavior can change; the Python CLI remains usable without an agent host.

## First coordination prompt

```text
Use cascade for <specification or ticket URLs>.
Work in <absolute workspace path> and follow its repository instructions.
Reuse the active tracker if one exists; otherwise create one at
<absolute workspace path>/.scratch/release/tracker.html.
Identify all tickets and current dependencies, prepare the ready prompts,
and hand them to me. Record blockers and the next useful action.
```

The skill prepares assignments by default. If you want delegation, explicitly state the allowed tickets, work surfaces and delivery boundaries. A prepared prompt is not a started mission. Your host's actual tools and permissions determine what can be dispatched.

`$implement` is a prompt convention used by the original workflow. It is not included in this repository. Point to your own implementation skill or substitute the project's explicit implementation instructions. Preserve the ticket, tracker pointer, scope, report destination and required delivery evidence.

## Maintain a tracker

Run the commands below from the clone. When working from an installed copy, use that copy's absolute `scripts/tracker.py` path.

```bash
python3 scripts/tracker.py init /tmp/release/tracker.html \
  --title "Release coordination" --spec "https://example.com/specification"
python3 scripts/tracker.py read /tmp/release/tracker.html > /tmp/release-edit.json
# Edit the JSON in your editor; retain _base_sha256.
python3 scripts/tracker.py save /tmp/release/tracker.html --data /tmp/release-edit.json
python3 scripts/tracker.py check /tmp/release/tracker.html
```

`read` attaches the HTML revision to temporary editing JSON. `save` checks that revision under a local lock, validates the data and exports prompts. If another writer changed the tracker, reread and reconcile their changes before saving. Use a new prompt filename for a changed follow-up; existing prompt exports are immutable.

The HTML contains the maintained state. The editing JSON is temporary. The browser offers search, state filters, criterion evidence, dependency links and prompt copying; it does not persist edits.

See [tracker format and prompt templates](../references/tracker-and-prompts.md) for field definitions. The [demo generator](../examples/create_demo.py) shows a complete small state with exported prompts and a local example report.

## Reconcile the result

Check every current ticket criterion against the returned report and relevant evidence. Keep author-reported claims distinct from independent observations. Update the same HTML before the next assignment.

| Mission state | Meaning |
| --- | --- |
| `to-prepare` | Scope or assignment is still being prepared. |
| `ready` | Prerequisites are satisfied and work can be assigned. |
| `running` | Execution was actually confirmed. |
| `to-verify` | A result needs the coordinator's verification. |
| `follow-up` | An observed remainder has a targeted next action. |
| `blocked` | A precise prerequisite or decision is missing. |
| `done` | Criteria and mandated delivery obligations have evidence. |

Prompt delivery is recorded separately as `prepared`, `sent` or `unknown`. Tests, push, CI, merge, deployment, target verification and acceptance are distinct facts. An unchanged blocker calls for a release condition and owner; automatic correction uses a finite, cumulative attempt budget.

## Resume after interruption

For delegated or multi-turn work, follow [bounded context recovery](../references/long-context.md): bind a stable workstream locator with `scripts/bootstrap.py`, keep the current mandate and action in the HTML, and resume against the expected SHA with `scripts/resume.py`.

Use the helpers' `--help` for exact arguments. A resumed pending send starts with receipt reconciliation. It does not automatically resend. Refresh the authoritative facts relevant to the next decision even when the recorded fingerprints are consistent.

## Optional guarded dispatch and remote recovery

Manual handoff needs no claim remote. For authorized delegation with this tracker, read [dispatch guards](../references/dispatch-guards.md) and [execution and recovery](../references/execution-and-recovery.md) before sending.

Select a separate repository owned by you or your team. Use a private operational repository for private prompts or evidence. Configure it through the tracker and workspace instructions, following [shared repository setup](../references/shared-repository.md). The public distribution repository is not a recovery destination.

Remote helpers require Git and access to your selected remote. GitHub source adapters additionally use authenticated `gh api` reads. Installing the skill neither signs you in nor provisions credentials.

For host loss, read [durable checkpoints](../references/durable-checkpoints.md). Publish the exact pending intent before sending, verify through a fresh fetch and complete a restore drill at another root. Preserve original payload bytes and stable request identities. A lost response remains unknown until the actual service establishes the outcome.

Use workstream, referenced evidence and temporary directory paths without spaces for checkpoint/recovery operations: the current local-reference parser does not cover every spaced-path form. Installation and basic tracker operations support paths with spaces. The full runtime test suite also expects a temporary root without spaces.

## Troubleshooting

| Symptom | Next action |
| --- | --- |
| `No module named fcntl` | Run the helper on Linux, macOS or inside WSL. |
| Existing installation destination | Install to a fresh path and compare your existing copy before replacing it. |
| Revision conflict on save | Reread the tracker and reconcile; retain other writers' facts. |
| Missing or divergent prompt export | Restore the original export or prepare a new follow-up filename, then save and check. |
| Skill not discovered | Confirm `SKILL.md` is directly inside the installed directory and consult your host's discovery rules. |
| Copy button unavailable | Expand the prompt, select its text and copy manually. |
| Source status is unknown | Fix access or refresh authoritative observations before dependent dispatch or acceptance. |

For bugs, include the command, Python/OS versions, a minimal sanitized tracker and the observed error. Keep credentials and private workstream data out of public issues.
