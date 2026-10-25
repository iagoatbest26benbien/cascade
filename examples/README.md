# Try the tracker

From the repository root:

```bash
python3 examples/create_demo.py /tmp/orchestrate-demo
python3 scripts/tracker.py check /tmp/orchestrate-demo/tracker.html
```

Open `/tmp/orchestrate-demo/tracker.html` in a modern browser. No server, network or account is needed. The destination must be new; choose another path to generate a second example.

The fictional Starling release contains four missions:

```text
T-001 · API contract · accepted
  ├── T-002 · export endpoint · ready
  └── T-003 · export experience · running
        T-002 + T-003 → T-004 · release verification · blocked
```

The scenario makes T-002 eligible for a manual prompt handoff, preserves T-003's existing fictional run, and leaves T-004 blocked on both implementations. T-001's evidence is a local report explicitly labeled synthetic.

Try searching for `export`, filtering `Ready`, following a dependency anchor and expanding the implementation prompt. Copy the prompt to inspect its tracker and evidence paths; it is marked as a demo and should not be sent to a real worker.

The generator creates the HTML, prompt export and report with absolute paths for your chosen directory. Its example URLs are intentionally non-operational. It creates no claims or checkpoints and invokes no dispatch tool.

To edit the generated tracker, follow the [revision-checked workflow](../docs/guide.md#maintain-a-tracker). Change follow-ups to a new prompt filename to preserve old exports.
