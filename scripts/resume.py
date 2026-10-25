#!/usr/bin/env python3
"""Read bounded, revision-bound views of a tracker without changing its state."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import types


SCHEMA = "orchestrate-tickets/resume-view/v1"
ACTION_PHASES = {"reconcile", "prepare", "dispatch_pending", "await_result", "verify", "await_decision", "idle"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def fits(value, budget):
    return len(encoded(value).encode("utf-8")) <= budget


def child(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def preview(value, path, omitted, depth=3):
    if isinstance(value, str):
        if len(value) > 240:
            omitted.add(path)
            return value[:240] + "…"
        return value
    if isinstance(value, (dict, list)):
        if depth == 0:
            omitted.add(path)
            return {"expand_pointer": path, "members": len(value)}
        if len(value) > 6:
            omitted.add(path)
        pairs = list(value.items())[:6] if isinstance(value, dict) else list(enumerate(value))[:6]
        items = [(key, preview(item, child(path, key), omitted, depth - 1)) for key, item in pairs]
        return dict(items) if isinstance(value, dict) else [item for _, item in items]
    return value


def select(data, pointer):
    require(pointer == "" or pointer.startswith("/"), "Use a JSON pointer beginning with /.")
    current = data
    for raw in pointer.split("/")[1:] if pointer else []:
        require(not re.search(r"~(?![01])", raw), "Invalid JSON pointer escape.")
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            require(key.isascii() and key.isdigit() and str(int(key)) == key and int(key) < len(current),
                    "Array pointer index is out of range or noncanonical.")
            current = current[int(key)]
        else:
            require(isinstance(current, dict) and key in current, "JSON pointer does not exist.")
            current = current[key]
    return current


def load(path):
    # Read/hash/parse the same bytes if another writer atomically replaces HTML.
    raw = path.read_bytes()
    trusted = Path(__file__).resolve().with_name("tracker.py")
    tracker = types.ModuleType("resume_tracker")
    tracker.__file__ = str(trusted)
    exec(compile(trusted.read_bytes(), str(trusted), "exec"), tracker.__dict__)
    matches = list(tracker.DATA.finditer(raw.decode("utf-8")))
    require(len(matches) == 1, "Expected exactly one tracker-data block.")
    data = json.loads(matches[0][2])
    tracker.validate(data, path.parent)
    return data, hashlib.sha256(raw).hexdigest()


def history_fingerprints(data):
    return {name + "_sha256": hashlib.sha256(json.dumps(data[name], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest() for name in ("journal", "decisions")}


def continuation_fingerprints(data):
    # Bind the checkpoint to the state it describes, including mission-only edits.
    # Save timestamps and editing metadata change without changing that meaning.
    state = {key: value for key, value in data.items()
             if key not in {"journal", "decisions", "updated_at", "_base_sha256", "continuation"}}
    contract = data.get("continuation")
    state["continuation"] = ({key: value for key, value in contract.items() if key != "reconciled"}
                             if isinstance(contract, dict) else contract)
    raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return dict(history_fingerprints(data), state_sha256=hashlib.sha256(raw).hexdigest())


def continuation_review(data):
    contract = data.get("continuation")
    if contract is None:
        return "missing", ["Recover continuation from applicable user instructions and recorded sources."]
    if not isinstance(contract, dict):
        return "incomplete", ["continuation must be an object."]
    gaps = []
    if not isinstance(contract.get("mandate"), str) or not contract["mandate"].strip():
        gaps.append("Recover continuation.mandate.")
    for field in ("sources", "constraints", "read_next"):
        values = contract.get(field)
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values) or (field == "sources" and not values):
            gaps.append("Recover continuation." + field + ".")
    action = contract.get("current_action")
    if not isinstance(action, dict):
        gaps.append("Recover continuation.current_action.")
    else:
        if not isinstance(action.get("phase"), str) or action["phase"] not in ACTION_PHASES:
            gaps.append("Recover current_action.phase.")
        ids = action.get("mission_ids")
        known = {m["id"] for m in data["missions"]}
        if not isinstance(ids, list) or not all(isinstance(mid, str) and mid in known for mid in ids):
            gaps.append("Recover current_action.mission_ids from this tracker.")
        if not isinstance(action.get("next_step"), str) or not action["next_step"].strip():
            gaps.append("Recover current_action.next_step.")
        sources = action.get("sources")
        if not isinstance(sources, list) or not sources or not all(isinstance(s, str) and s.strip() for s in sources):
            gaps.append("Recover current_action.sources.")
    reconciled = contract.get("reconciled")
    fingerprints = continuation_fingerprints(data)
    if not isinstance(reconciled, dict) or any(not isinstance(reconciled.get(k), str) or
            not re.fullmatch(r"[0-9a-f]{64}", reconciled[k]) for k in fingerprints):
        gaps.append("Recover continuation.reconciled history/state fingerprints after reviewing applicable changes.")
    if gaps:
        return "incomplete", gaps
    changed = [k for k, value in fingerprints.items() if reconciled[k] != value]
    if changed:
        return "stale", ["Reconcile changed " + k + " before updating the action checkpoint." for k in changed]
    return "recorded", []


def contract_status(data):
    return continuation_review(data)[0]


def exhausted(record):
    return (isinstance(record, dict) and type(record.get("used")) is int
            and type(record.get("limit")) is int and 0 <= record["limit"] <= record["used"])


def mission_flags(mission):
    dispatch = mission.get("dispatch")
    prompt = mission.get("prompt") or {}
    context = dispatch if isinstance(dispatch, dict) else {}
    attempts = mission.get("attempts")
    attempts = attempts if isinstance(attempts, dict) else {}
    hold = mission.get("hold")
    flags = []
    if "unknown" in (prompt.get("delivery"), context.get("status"), context.get("delivery")):
        flags.append("uncertain_delivery")
    if context.get("claim_state") in {"acquiring", "held"}:
        flags.append("unreleased_claim")
    if hold and not (isinstance(hold, dict) and hold.get("active") is False):
        flags.append("recorded_hold")
    if any(exhausted(value) for value in attempts.values()):
        flags.append("exhausted_budget")
    if mission["status"] == "running" or context.get("status") in ("running", "accepted", "queued"):
        flags.append("recorded_active_run")
    if mission.get("blocker"):
        flags.append("recorded_blocker")
    if any(not isinstance(value, dict) or type(value.get("used")) is not int
           or type(value.get("limit")) is not int or value["used"] < 0 or value["limit"] < 0
           for value in attempts.values()):
        flags.append("attempt_limit_unknown")
    current_execution = mission["status"] != "done" or "uncertain_delivery" in flags or "recorded_active_run" in flags
    if current_execution and (mission["status"] == "running" or prompt.get("delivery") in {"sent", "unknown"}) and not any(
            context.get(key) for key in ("request_id", "request", "idempotency_key", "task_id", "run_id")):
        flags.append("execution_identity_missing")
    if prompt.get("kind") == "follow-up" and not attempts:
        flags.append("attempt_history_missing")
    return flags


def base_view(revision):
    return {"schema": SCHEMA, "view_only": True, "revision_sha256": revision,
            "external_state": "not refreshed; recorded state is not dispatch or acceptance authority"}


def budget_failure(revision, budget):
    return dict(base_view(revision), budget_exceeded=True, max_bytes=budget,
                next_action="Select a narrower --pointer at this --expected revision; views are never save input.")


def overview(data, revision, offset, limit, budget):
    omitted = set()
    result = base_view(revision)
    for key in ("title", "spec", "scope", "updated_at", "next_action", "continuation"):
        result[key] = preview(data.get(key), "/" + key, omitted)
    result["continuation_status"], result["continuation_recovery_work"] = continuation_review(data)
    result["continuation_requires_expansion"] = any(p.startswith("/continuation") for p in omitted)
    result["history"] = {"journal_entries": len(data["journal"]), "journal_pointer": "/journal",
                         "decision_entries": len(data["decisions"]), "decisions_pointer": "/decisions"}
    result["history"].update(history_fingerprints(data))
    result["reconciliation_fingerprints"] = continuation_fingerprints(data)
    result["mission_counts"] = dict(Counter(m["status"] for m in data["missions"]))
    flagged = [(i, m, mission_flags(m)) for i, m in enumerate(data["missions"])]
    result["recorded_flags"] = dict(Counter(flag for _, _, flags in flagged for flag in flags))
    result["flag_limits"] = "Counts cover all missions, including done; unstructured constraints and journal prose are not classified. Missing metadata is not zero attempts or permission."
    if "automatic_correction_budget" in data:
        result["global_budget"] = preview(data["automatic_correction_budget"], "/automatic_correction_budget", omitted)
        result["global_budget_exhausted"] = exhausted(data["automatic_correction_budget"])
    priorities = {"uncertain_delivery": 32, "recorded_hold": 16, "exhausted_budget": 8,
                  "execution_identity_missing": 4, "recorded_active_run": 2}
    relevant = [(i, m, flags) for i, m, flags in flagged if m["status"] != "done" or flags]
    relevant.sort(key=lambda row: (-sum(priorities.get(flag, 0) for flag in row[2]), row[0]))
    require(offset <= len(relevant), "Overview offset is out of range.")
    rows = []
    for index, mission, flags in relevant[offset:offset + limit]:
        pointer = "/missions/" + str(index)
        row = {"pointer": pointer, "flags": flags}
        for key in ("id", "title", "status", "owner", "workspace", "depends_on", "blocker", "next_action",
                    "checked_at", "report", "work_revision", "dispatch", "attempts", "hold", "guard"):
            if key in mission:
                row[key] = preview(mission[key], child(pointer, key), omitted)
        if mission.get("prompt"):
            row["prompt"] = {key: mission["prompt"][key] for key in ("kind", "delivery", "file")}
            row["prompt"] = preview(row["prompt"], child(pointer, "prompt"), omitted)
        rows.append(row)
    result["page"] = {"offset": offset, "total": len(relevant), "returned": len(rows),
                      "next_offset": offset + len(rows) if offset + len(rows) < len(relevant) else None}
    result["missions"] = rows
    result["expand_pointers"] = sorted(omitted)
    result["partial"] = bool(omitted) or offset > 0 or result["page"]["next_offset"] is not None
    while rows and not fits(result, budget):
        rows.pop()
        result["page"].update(returned=len(rows), next_offset=offset + len(rows))
        result["partial"] = True
    if not fits(result, budget) or not rows and offset < len(relevant):
        return budget_failure(revision, budget)
    return result


def page(data, revision, pointer, offset, limit, contains, budget):
    value = select(data, pointer)
    result = dict(base_view(revision), pointer=pointer, offset=offset)
    if isinstance(value, str):
        require(contains is None, "--contains is for a list of journal/decision strings.")
        require(offset <= len(value), "String offset is out of range.")
        end = min(offset + (limit or 2000), len(value))
        while True:
            result.update(text=value[offset:end], total=len(value), returned=end - offset,
                          next_offset=end if end < len(value) else None, partial=offset > 0 or end < len(value))
            if fits(result, budget):
                if end == offset and end < len(value):
                    return budget_failure(revision, budget)
                return result
            require(end > offset, "Output budget is too small.")
            end = offset + (end - offset) // 2
    require(contains is None or isinstance(value, list) and all(isinstance(v, str) for v in value),
            "--contains requires a list of strings.")
    if isinstance(value, (dict, list)):
        pairs = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        if contains is not None:
            pairs = [(key, item) for key, item in pairs if contains in item]
        require(offset <= len(pairs), "Page offset is out of range.")
        omitted = set()
        items = [{"pointer": child(pointer, key), "value": preview(item, child(pointer, key), omitted)}
                 for key, item in pairs[offset:offset + (limit or 10)]]
        result.update(total=len(pairs), items=items, expand_pointers=sorted(omitted))
        while True:
            end = offset + len(items)
            result.update(returned=len(items), next_offset=end if end < len(pairs) else None,
                          partial=bool(omitted) or offset > 0 or end < len(pairs))
            if fits(result, budget) and (items or offset == len(pairs)):
                return result
            if not items:
                return budget_failure(revision, budget)
            items.pop()
    require(offset == 0 and contains is None, "Scalar values have no page offset or search.")
    result.update(value=value, partial=False)
    return result if fits(result, budget) else budget_failure(revision, budget)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--pointer", help="JSON pointer, for example /missions/7/dispatch")
    selection.add_argument("--mission", help="Exact stable mission ID; pages its fields")
    parser.add_argument("--expected", help="Required HTML SHA-256 for any selected detail")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, help="Overview records, array/object members, or exact string characters")
    parser.add_argument("--contains", help="Literal substring search in a selected list of strings")
    parser.add_argument("--max-bytes", type=int, default=12000)
    parser.add_argument("--require-continuation", action="store_true",
                        help="Reject missing, incomplete or history/state-stale action checkpoints; does not authorize dispatch")
    args = parser.parse_args(argv)
    require(args.offset >= 0 and (args.limit is None or 1 <= args.limit <= 10000), "Invalid offset/limit.")
    require(2048 <= args.max_bytes <= 65536, "--max-bytes must be between 2048 and 65536.")
    data, revision = load(args.html.resolve())
    selected = args.pointer is not None or args.mission is not None
    require(not (selected or args.offset > 0 or args.require_continuation) or args.expected is not None,
            "Selected or subsequent page reads require --expected from a fresh overview.")
    require(args.expected is None or args.expected == revision, "Stale tracker revision; refresh the overview before following its pointers.")
    if args.require_continuation:
        status, gaps = continuation_review(data)
        require(status == "recorded", "Continuation " + status + ": " + " ".join(gaps))
    if args.mission is not None:
        indexes = [i for i, mission in enumerate(data["missions"]) if mission["id"] == args.mission]
        require(indexes, "Unknown mission ID.")
        args.pointer = "/missions/" + str(indexes[0])
    if selected:
        result = page(data, revision, args.pointer, args.offset, args.limit, args.contains, args.max_bytes)
    else:
        require(args.contains is None, "--contains requires a selected list pointer.")
        result = overview(data, revision, args.offset, min(args.limit or 8, 100), args.max_bytes)
    output = encoded(result)
    require(len(output.encode("utf-8")) <= args.max_bytes, "Output budget exceeded.")
    sys.stdout.write(output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TypeError, KeyError, UnicodeError) as error:
        raise SystemExit(str(error))
