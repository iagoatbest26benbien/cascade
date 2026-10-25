#!/usr/bin/env python3
"""Create a fictional, offline four-ticket workstream in a new directory."""

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tracker


def create(destination):
    root = destination.expanduser().resolve()
    if root.exists():
        raise ValueError(f"Destination already exists: {root}; choose a new directory.")
    root.mkdir(parents=True)
    report = root / "evidence" / "T-001" / "REPORT.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "# Fictional acceptance report\n\n"
        "This is synthetic demonstration data, not a record of real execution.\n\n"
        "The example contract and its three sample consumers were reviewed. "
        "T-001 is accepted only within this fictional scenario.\n",
        encoding="utf-8",
    )
    path = root / "tracker.html"
    template = Path(__file__).resolve().parents[1] / "assets" / "tracker.html"
    data = {
        "title": "A calmer path to release.",
        "spec": "https://example.com/fictional-starling-release",
        "scope": "Starling / release 2.4 — Fictional demo. Four tickets, one shared view, and a clear next action.",
        "updated_at": "2026-10-25T12:00:00+00:00",
        "next_action": "Hand off T-002: the API contract is accepted and the export work surface is available.",
        "decisions": [],
        "missions": [],
        "journal": [
            "2026-10-25 — Synthetic demo initialized. All tickets, owners, execution states and evidence are fictional. No assignments were sent.",
        ],
    }

    def mission(mid, title, status, owner, dependencies, action):
        return {
            "id": mid, "title": title,
            "ticket": "https://example.com/tickets/" + mid,
            "status": status, "owner": owner, "chat": "", "workspace": "Starling · " + mid.lower(),
            "checked_at": "2026-10-25T12:00:00+00:00", "depends_on": dependencies,
            "criteria": [], "evidence": [], "remaining": [], "blocker": "",
            "next_action": action, "report": "",
        }

    export = mission("T-002", "Build the export endpoint", "ready", "API agent (example)", ["T-001"],
                     "Copy the prepared prompt and hand it to the implementer.")
    export["criteria"] = [
        {"text": "Export the selected date range with the accepted response contract.", "result": "unverified", "proof": ""},
        {"text": "Provide a focused integration test and a report for verification.", "result": "unverified", "proof": ""},
    ]
    export["remaining"] = ["Implementation, integration evidence and coordinator verification."]
    export["prompt"] = {
        "kind": "implementation", "file": "prompts/T-002-implementation.md", "delivery": "prepared",
        "text": "$implement https://example.com/tickets/T-002\n\n"
                "FICTIONAL DEMO — do not send this example to a real worker.\n\n"
                f"Read {path}, mission T-002, then the ticket and repository instructions. "
                "Implement the accepted export contract and all ticket criteria.\n\n"
                f"Return the report and evidence under {root / 'evidence' / 'T-002'}/. "
                "Separate local checks, remote delivery and remaining acceptance work. "
                "The coordinator maintains the tracker.",
    }
    ui = mission("T-003", "Design the export experience", "running", "UI agent (example)", ["T-001"],
                 "Await the existing example run, then verify the returned UI evidence.")
    ui["criteria"] = [{"text": "Handle loading, empty results and retry feedback.", "result": "unverified", "proof": ""}]
    ui["evidence"] = ["Fictional scenario: an existing UI run was confirmed. No actual worker is running."]
    ui["remaining"] = ["Implementation report and browser verification."]
    release = mission("T-004", "Verify the complete release", "blocked", "Release coordinator (example)", ["T-002", "T-003"],
                      "Wait for both implementation reports and accepted delivery evidence.")
    release["criteria"] = [{"text": "Verify export → storage → canonical reread on the required target.", "result": "unverified", "proof": ""}]
    release["blocker"] = "T-002 and T-003 must be accepted. Owner: release coordinator. Release condition: verified API and UI delivery."
    release["remaining"] = ["Target verification and final acceptance."]
    contract = mission("T-001", "Agree the API contract", "done", "Coordinator (example)", [],
                       "Use the accepted contract for the two independent implementation tickets.")
    contract["criteria"] = [{"text": "Record and review the export response contract.", "result": "passed", "proof": str(report)}]
    contract["evidence"] = ["Synthetic report: " + str(report)]
    contract["report"] = str(report)
    data["missions"] = [export, ui, release, contract]
    exports = tracker.validate(data, root)
    for filename, text in exports.items():
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(text, encoding="utf-8")
    path.write_text(tracker.render(template.read_text(encoding="utf-8"), data), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New workstream directory; existing paths are never overwritten")
    args = parser.parse_args()
    try:
        path = create(args.destination)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Demo creation failed: {error}\n")
    print(f"Fictional demo created: {path}\nOpen that file in your browser. Nothing was sent.")


if __name__ == "__main__":
    main()
