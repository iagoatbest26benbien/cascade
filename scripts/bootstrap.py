#!/usr/bin/env python3
"""Bind and locate workspace-relative HTML trackers through one AGENTS.md block."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import sys
import tempfile
import types
from urllib.parse import urlsplit


SCHEMA = "orchestrate-tickets/bootstrap/v1"
START = b"<!-- orchestrate-tickets:bootstrap:start -->"
END = b"<!-- orchestrate-tickets:bootstrap:end -->"
SCRIPT = Path(__file__).resolve()
ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)+")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fingerprint(info):
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def no_symlinks(path):
    for part in (*reversed(path.parents), path):
        require(not part.is_symlink(), f"Symlink is not allowed: {part}")


def workspace_path(value):
    path = Path(os.path.abspath(value))
    no_symlinks(path)
    require(path.is_dir(), "Workspace must be an existing directory.")
    return path


def relative_path(value):
    require(isinstance(value, str) and value and "\\" not in value and "\0" not in value,
            "Tracker must be a workspace-relative path.")
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value
            and ".." not in path.parts and value != ".",
            "Tracker path must be canonical and inside the workspace.")
    return value


def tracker_path(root, value):
    path = root / relative_path(value)
    no_symlinks(path)
    return path


def safe_read(path, missing=False):
    no_symlinks(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        require(missing, f"Missing file: {path}")
        return None, None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                f"Expected a regular file with one link: {path}")
        raw = stream.read()
        require(fingerprint(before) == fingerprint(os.fstat(stream.fileno())),
                f"File changed while reading: {path}")
    return raw, before


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(raw):
    def invalid(value):
        raise ValueError(f"Invalid JSON constant: {value}")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid)


def instruction():
    installed = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills/cascade/SKILL.md"
    shared = Path.home() / ".agents/skills/cascade/SKILL.md"
    candidates = (SCRIPT.parent.parent / "SKILL.md", shared, installed,
                  SCRIPT.parent.parent / "docs/long-context.md")
    reference = next((f"`{path}`" for path in candidates if path.is_file()),
                     "the available installed `cascade/SKILL.md` or bundled `docs/long-context.md`")
    return (
        f"After interruption or compaction, reread {reference}. Use the bundled sibling helpers: "
        f"`python -B {shlex.quote(str(SCRIPT))} locate WORKSPACE`, then "
        f"`python -B {shlex.quote(str(SCRIPT.with_name('resume.py')))} SELECTED_TRACKER`. "
        "Select by canonical source when ambiguous; reconcile recorded pending sends before action.\n"
    ).encode()


def source(value):
    if isinstance(value, str) and value.startswith("workspace:"):
        relative_path(value[len("workspace:"):])
        return value
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`]+", value),
            "Canonical source must be a clean URL.")
    parsed = urlsplit(value)
    require(parsed.scheme.lower() != "file" and parsed.netloc and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment,
            "Canonical source must be a clean URL without credentials, query or fragment.")
    return value


def spec_source(root, tracker, value):
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return source(value)
    path = Path(value)
    path = Path(os.path.abspath(path if path.is_absolute() else tracker.parent / path))
    require(path.is_relative_to(root), "Local spec must be inside the workspace or have an explicit canonical workstream source.")
    safe_read(path)
    return source("workspace:" + path.relative_to(root).as_posix())


def validate_entries(entries):
    require(isinstance(entries, list) and entries, "Expected a nonempty locator list.")
    ids, paths = set(), set()
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {"id", "tracker", "source"},
                "Locators contain only id, tracker and source.")
        require(isinstance(entry["id"], str) and ID.fullmatch(entry["id"]),
                "Workstream ID must be project/stable-workstream.")
        relative_path(entry["tracker"])
        source(entry["source"])
        require(entry["id"] not in ids and entry["tracker"] not in paths,
                "Duplicate locator ID or tracker path.")
        ids.add(entry["id"])
        paths.add(entry["tracker"])


def parse_block(raw):
    starts, ends = raw.count(START), raw.count(END)
    require(raw.count(b"<!-- orchestrate-tickets:bootstrap:") == starts + ends,
            "Malformed bootstrap marker.")
    if starts == ends == 0:
        return [], None
    require(starts == ends == 1, "Expected one complete bootstrap block.")
    begin, end = raw.index(START), raw.index(END)
    require(begin < end, "Malformed bootstrap marker order.")
    body = raw[begin + len(START):end]
    # Instructions are not executable input; preserve compatibility with moved installations.
    match = re.fullmatch(rb"\n([^\n]+)\n```json\n(.*?)\n```\n", body, re.S)
    require(match is not None and match[1].startswith(b"After interruption or compaction,"),
            "Malformed bootstrap block.")
    data = strict_json(match[2])
    require(isinstance(data, dict) and set(data) == {"schema", "locators"}
            and data["schema"] == SCHEMA, "Unknown bootstrap schema.")
    validate_entries(data["locators"])
    return data["locators"], (begin, end + len(END))


def block_bytes(entries):
    payload = json.dumps({"schema": SCHEMA, "locators": entries}, ensure_ascii=True,
                         separators=(",", ":")).encode()
    return START + b"\n" + instruction() + b"```json\n" + payload + b"\n```\n" + END


def read_tracker(root, entry):
    path = tracker_path(root, entry["tracker"])
    raw, _ = safe_read(path)
    # The helper belongs to this trusted installation/bundle, not the target tracker.
    trusted = SCRIPT.with_name("tracker.py")
    tracker = types.ModuleType("bootstrap_tracker")
    tracker.__file__ = str(trusted)
    exec(compile(trusted.read_bytes(), str(trusted), "exec"), tracker.__dict__)
    matches = list(tracker.DATA.finditer(raw.decode("utf-8")))
    require(len(matches) == 1, "Expected exactly one tracker-data block.")
    data = strict_json(matches[0][2])
    tracker.validate(data, path.parent)
    require(isinstance(data["spec"], str) and data["spec"].strip(), "Tracker spec is required.")
    if "workstream" in data:
        identity = data["workstream"]
        require(isinstance(identity, dict) and set(identity) == {"id", "source"},
                "Invalid tracker workstream identity.")
        require(identity["id"] == entry["id"], "Tracker workstream ID mismatch.")
        actual_source = source(identity["source"])
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", data["spec"]) and not actual_source.startswith("workspace:"):
            # Explicit URL identity can also name a verified local specification.
            local = Path(data["spec"])
            safe_read(local if local.is_absolute() else path.parent / local)
        else:
            require(spec_source(root, path, data["spec"]) == actual_source, "Tracker spec/source mismatch.")
    else:
        actual_source = spec_source(root, path, data["spec"])
    require(entry.get("source", actual_source) == actual_source, "Tracker canonical source mismatch.")
    return dict(entry, source=actual_source, revision_sha256=digest(raw))


def view(root, raw, selected=None, choices=None):
    return {"schema": "orchestrate-tickets/bootstrap-view/v1", "workspace": str(root),
            "agents_sha256": digest(raw) if raw is not None else None,
            "selected": selected, "choices": choices or []}


def locate(workspace, workstream_id=None):
    root = workspace_path(workspace)
    raw, _ = safe_read(root / "AGENTS.md")
    entries, _ = parse_block(raw)
    if not entries and workstream_id is None:
        return view(root, raw)
    if workstream_id is None and len(entries) > 1:
        return view(root, raw, choices=entries)
    matches = [entry for entry in entries if workstream_id is None or entry["id"] == workstream_id]
    require(len(matches) == 1, "Unknown workstream ID; inspect the registered choices.")
    return view(root, raw, selected=read_tracker(root, matches[0]))


@contextmanager
def writer_lock(root):
    # Lock the stable workspace directory inode: no second register or lock sidecar.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another bootstrap writer holds the workspace lock; reread before retrying.") from None
        yield fd
    finally:
        os.close(fd)


def atomic_replace(path, raw, original, original_stat, directory_fd):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".bootstrap-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), stat.S_IMODE(original_stat.st_mode) if original_stat else 0o644)
            os.fsync(stream.fileno())
        current, current_stat = safe_read(path, missing=True)
        require(current == original and fingerprint(current_stat) == fingerprint(original_stat),
                "AGENTS.md changed during bind; reread before retrying.")
        os.replace(temporary, path)
        os.fsync(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def bind(workspace, html, workstream_id, expected=None):
    root = workspace_path(workspace)
    require(isinstance(workstream_id, str) and ID.fullmatch(workstream_id),
            "Workstream ID must be project/stable-workstream.")
    candidate = Path(html)
    if candidate.is_absolute():
        require(candidate.is_relative_to(root), "Tracker must be inside the workspace.")
        html = candidate.relative_to(root).as_posix()
    else:
        html = str(html)
    relative_path(html)
    path = root / "AGENTS.md"
    with writer_lock(root) as directory_fd:
        original, original_stat = safe_read(path, missing=True)
        require(expected is None or re.fullmatch(r"[a-f0-9]{64}", expected), "Invalid expected AGENTS SHA.")
        require(original is None and expected is None or original is not None and expected == digest(original),
                "Stale or missing expected AGENTS SHA; run locate/read AGENTS.md before binding.")
        entries, span = parse_block(original or b"")
        selected = read_tracker(root, {"id": workstream_id, "tracker": html})
        entry = {key: selected[key] for key in ("id", "tracker", "source")}
        conflicts = [old for old in entries if old["id"] == workstream_id or old["tracker"] == html]
        require(not conflicts or conflicts == [entry], "Existing locator ID/path differs; reconcile explicitly.")
        if not conflicts:
            entries.append(entry)
        block = block_bytes(entries)
        if span is None:
            updated = (original or b"") + b"\n" + block + b"\n"
        else:
            updated = original[:span[0]] + block + original[span[1]:]
        if updated != original:
            atomic_replace(path, updated, original, original_stat, directory_fd)
        return view(root, updated, selected=selected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    binding = commands.add_parser("bind")
    binding.add_argument("workspace")
    binding.add_argument("--tracker", required=True)
    binding.add_argument("--id", required=True)
    binding.add_argument("--expected")
    locating = commands.add_parser("locate")
    locating.add_argument("workspace")
    locating.add_argument("--id")
    args = parser.parse_args(argv)
    if args.command == "bind":
        result = bind(args.workspace, args.tracker, args.id, args.expected)
    else:
        result = locate(args.workspace, args.id)
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TypeError, KeyError, UnicodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(1)
